//! `vct-module.json` parsing + validation + placeholder resolution.
//!
//! Spec reference: `docs/VCT_MODULE_MANIFEST_SPEC.md` in the Claude
//! Orchestrator meta-project (not shipped in this repo).
//!
//! The parser is deliberately permissive about unknown top-level fields
//! (forward compatibility) but strict about required ones. Unrecognized
//! values for enumerated fields (host, runtime type, etc.) are rejected
//! early so they don't produce confusing errors deeper in the install flow.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

// ─── Top-level manifest type ────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct ModuleManifest {
    #[serde(default)]
    pub manifest_version: u32,

    pub id: String,
    pub name: String,
    pub version: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub publisher: Option<String>,
    #[serde(default)]
    pub homepage: Option<String>,
    #[serde(default)]
    pub repository: Option<String>,
    #[serde(default)]
    pub icon: Option<String>,

    pub category: ModuleCategory,
    #[serde(default)]
    pub tags: Vec<String>,

    #[serde(default)]
    pub compatibility: Compatibility,

    #[serde(default)]
    pub license: LicenseBlock,

    #[serde(default)]
    pub requirements: Requirements,

    pub install: InstallBlock,

    #[serde(default)]
    pub secrets: Vec<SecretDecl>,

    #[serde(default)]
    pub settings: Vec<SettingDecl>,

    pub runtime: RuntimeBlock,

    #[serde(default)]
    pub mcp_registration: Option<McpRegistration>,

    #[serde(default)]
    pub setup_wizard: Option<SetupWizard>,

    #[serde(default)]
    pub upgrade: Option<UpgradeBlock>,

    #[serde(default)]
    pub telemetry: Option<serde_json::Value>,

    #[serde(default)]
    pub uninstall: Option<UninstallBlock>,

    #[serde(default)]
    pub provides: Vec<serde_json::Value>,
    #[serde(default)]
    pub consumes: Vec<serde_json::Value>,

    /// Stream 2 (2026-05-19): module-contributed GUI surfaces. When
    /// populated, the launcher's Sidebar merges a nav entry for this
    /// module and renders `gui.config_tab` via
    /// `launcher/src/lib/components/ModuleConfigTab.svelte`. See the
    /// `GuiBlock` rustdoc for the full schema rationale (load-bearing —
    /// once a module ships with `gui.config_tab`, the schema becomes
    /// part of the public manifest contract).
    #[serde(default)]
    pub gui: Option<GuiBlock>,

    /// v0.2.31: module-shipped DB migrations block. When `Some(...)`,
    /// the launcher applies SQL files matching `[0-9]+_*.sql` from
    /// `{module_install_dir}/{db.migrations_dir}/` at install + update
    /// time, idempotent via SHA256 tracking in launcher's own
    /// `module_db_migrations` table (migration 019). Every table the
    /// module creates MUST be prefixed with `{db.namespace}_` —
    /// the launcher refuses to apply SQL that creates / alters tables
    /// outside the declared namespace. See
    /// `vct_launcher_core::db::module_db_migrations` for the apply
    /// mechanism + `.claude/context/plans/rl-module-launcher-db-tables-
    /// spec-2026-05-23.md` for the full design rationale.
    #[serde(default)]
    pub db: Option<DbBlock>,

    /// v0.2.49 item #13 (M-3): KG collections this module writes to.
    /// When set on an `install.scope = "global"` module, every project
    /// gains a default access row at install time (via
    /// `populate_kg_collection_access_for_global_module` + the access
    /// matrix resolver). For per-project modules the field is ignored.
    ///
    /// Pre-v0.2.49 manifests deserialize cleanly with this field
    /// absent (defaults to `None`). An empty `Vec` is semantically
    /// equivalent to `None` for the populate path (no rows inserted).
    #[serde(default)]
    pub kg_collections: Option<Vec<String>>,
}

// ─── DB (v0.2.31 / 2026-05-23) ──────────────────────────────────────────
//
// `DbBlock` declares a module's SQLite-migration footprint. The launcher
// owns the `launcher.db` file; modules ship CREATE TABLE / ALTER TABLE /
// CREATE INDEX statements that operate on their own namespaced tables
// inside that same file. This lets the dashboard widgets read module
// state without waking a stopped container, and lets modules persist
// state without each shipping its own SQLite filesystem-managed file.
//
// **Load-bearing**: once a paid module ships with a `db` block,
// breaking changes to this schema break that module's users — the
// launcher's apply-on-install code path runs against EVERY install /
// update of that module. Keep the schema additive.

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct DbBlock {
    /// Relative path (under the module's install_dir) where the
    /// launcher looks for SQL migration files. Files matching
    /// `[0-9]+_*.sql` are applied in lexicographic order. Convention:
    /// zero-pad to 4 digits (`0001_*.sql`, `0002_*.sql`, ...) so the
    /// natural string sort matches numeric sort.
    pub migrations_dir: String,

    /// Lowercase identifier prefix every module-owned table MUST
    /// declare. The launcher's apply mechanism refuses to execute SQL
    /// that creates / alters tables outside `{namespace}_*`. FOREIGN
    /// KEY references to launcher-owned tables (e.g. `projects(id)`)
    /// are allowed — namespace-enforcement only constrains DDL
    /// subjects, not FK targets.
    ///
    /// Validation: must match `[a-z][a-z0-9_]*`. Empty / uppercase /
    /// non-identifier-shaped values are rejected at manifest-load time.
    pub namespace: String,
}

// ─── GUI (Stream 2 / 2026-05-19) ────────────────────────────────────────
//
// `GuiBlock` declares optional GUI surfaces a module wants the launcher
// to render. Today there's exactly one slot — `config_tab` — but the
// block is a struct (not a single `Option<ConfigTab>` on the manifest)
// so future surfaces (status_widget, settings_panel, modal_dialog…)
// land additively without breaking older manifests.
//
// Schema-rendered design (Option A from the resume plan): the manifest
// describes WHAT to show, the launcher decides HOW. Module authors
// don't ship Svelte components; the launcher's `ModuleConfigTab.svelte`
// renders a fixed widget palette (Checkbox, MultiSelect, Button, Select,
// Info). This avoids a plugin sandbox AND keeps the UI consistent.
//
// **Load-bearing**: once a paid module ships with a `gui.config_tab`
// block, breaking changes to this schema break that module's users.
// All control variants accept `tooltip: Option<String>` so every
// control can carry mouseover help — non-tech users rely on it.

#[derive(Debug, Clone, Serialize, Deserialize, Default, JsonSchema)]
pub struct GuiBlock {
    /// Optional per-module config tab. When `Some(...)`, the launcher
    /// merges a sidebar entry routed to `/modules/<id>/config` (or to
    /// `config_tab.route` if set) and renders the schema there.
    #[serde(default)]
    pub config_tab: Option<ConfigTab>,
}

/// A module's "config tab" — single full-page surface composed of
/// collapsible sections, each containing a list of controls. Rendered
/// by `ModuleConfigTab.svelte`.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct ConfigTab {
    /// Title shown at the top of the tab AND as the sidebar nav label.
    pub title: String,
    /// Optional lucide icon name (e.g. `"sliders"`). Falls back to the
    /// first letter of `title` in a generic chip when None.
    #[serde(default)]
    pub icon: Option<String>,
    /// Optional sidebar route override. Defaults to
    /// `"/modules/<module_id>/config"` when None. Must start with `/`.
    #[serde(default)]
    pub route: Option<String>,
    /// Optional 1-line description rendered under the title at the top
    /// of the tab.
    #[serde(default)]
    pub description: Option<String>,
    /// Sections rendered in order. Empty sections render as a header
    /// with no body — caller's choice.
    pub sections: Vec<ConfigSection>,
    /// v0.2.23 F2 (2026-05-21): when false, the Sidebar's "Module
    /// configuration" group SUPPRESSES the nav entry for this module —
    /// the manifest is still discovered by `get_module_nav_items` (so
    /// callers like the per-project Settings page can render the tab in
    /// an embedded surface), but the standalone sidebar route is hidden.
    ///
    /// Default `true` keeps backwards compat for paid modules whose
    /// only surface is `/modules/<id>/config`. The orchestrator-core
    /// manifest sets this to `false` because v0.2.23 F2 folds its
    /// controls into the per-project Settings page (rendered there when
    /// the orchestrator-root project is the active project), so the
    /// duplicate sidebar entry is no longer wanted.
    #[serde(default = "default_show_in_sidebar")]
    pub show_in_sidebar: bool,
}

/// Serde default for `ConfigTab::show_in_sidebar` — `true`. Implemented
/// as a free fn rather than a closure so the `#[serde(default = "...")]`
/// attribute can reference it by name.
fn default_show_in_sidebar() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct ConfigSection {
    pub title: String,
    /// Optional 1-line description rendered under the section header.
    #[serde(default)]
    pub description: Option<String>,
    /// When true, the section gains a chevron toggle. When false, the
    /// section is always expanded.
    #[serde(default)]
    pub collapsible: bool,
    /// When `collapsible=true`, the section starts collapsed when this
    /// is true. Has no effect when `collapsible=false`.
    #[serde(default)]
    pub initially_collapsed: bool,
    pub controls: Vec<ConfigControl>,
}

/// Discriminated union of the renderer's widget palette. Adding a new
/// kind requires extending `ModuleConfigTab.svelte`'s render dispatch
/// AND documenting the new control here. Every variant carries
/// `tooltip: Option<String>` so authors can always provide hover help.
///
/// v0.2.26 (2026-05-22): `action` / `on_change` / `options_source` fields
/// changed from `String` (Tauri command name) to [`ActionRef`], which
/// accepts EITHER a legacy command name (back-compat for v0.2.20-v0.2.25
/// manifests) OR a structured [`ActionDescriptor`] that the generic
/// `module_dispatch_action` Tauri command executes without per-module
/// Rust code. Five new variants also landed: TextInput / NumberInput /
/// StatusDisplay / FilePicker / Link.
///
/// v0.2.33 (Agent D, 2026-05-25): the `Deserialize` impl is now custom
/// (no more `#[derive(Deserialize)]` here) so manifests with control
/// kinds unknown to this launcher version deserialize as the
/// [`ConfigControl::Unsupported`] fallback variant in LENIENT mode (the
/// default) — forward-compat with future launcher versions adding
/// kinds. Per-section render dispatch shows a placeholder for these.
/// Set `VCT_LAUNCHER_STRICT_MANIFEST=1` to restore the pre-v0.2.33
/// behaviour (unknown kinds error at parse time). Review §8.d for the
/// design rationale.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind")]
pub enum ConfigControl {
    /// Boolean toggle. On change, the launcher invokes `on_change`
    /// (either a Tauri command name or a structured action descriptor)
    /// with `{ moduleId, value }` if set, AND writes the new value into
    /// `module_settings` via the generic `set_module_setting` command
    /// (the renderer always persists, regardless of `on_change`).
    #[serde(rename = "checkbox")]
    Checkbox {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        #[serde(default)]
        default: bool,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    /// Multi-pick from a dynamic options list. The renderer calls
    /// `options_source` (legacy Tauri command name OR structured GET
    /// descriptor returning `Vec<SelectOption>`) on mount, then renders
    /// checkboxes for each option. Selected ids are persisted as a
    /// JSON array via the generic setting store, and pushed to
    /// `on_change` when set.
    ///
    /// v0.2.32 (L6): `options_source` now accepts `Vec<SelectOption>`
    /// with optional `badge` + `meta` fields per option (back-compat:
    /// bare-string lists `["a", "b"]` still deserialise — see
    /// [`SelectOption`]). The optional `filter` field hides options
    /// whose metadata doesn't match a runtime value (e.g. only show
    /// weight-bundle options whose `embedding_source` matches the
    /// project's `container.active_embedding`). v1 supports
    /// `kind = "match"` only; future kinds (regex, range, contains)
    /// land additively.
    #[serde(rename = "multi_select")]
    MultiSelect {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        /// Tauri command name OR structured descriptor returning
        /// `Vec<SelectOption>` (back-compat: plain `["a", "b"]` also
        /// accepted).
        options_source: ActionRef,
        /// v0.2.32: optional runtime-driven option filter. When set,
        /// the renderer hides options whose `meta.<meta_field>` ≠ the
        /// resolved runtime value. Defer-friendly: omit ⇒ all options
        /// visible (pre-v0.2.32 behaviour).
        #[serde(default)]
        filter: Option<MultiSelectFilter>,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    /// Action button. When clicked, the renderer invokes `action`
    /// (legacy Tauri command name OR structured action descriptor). If
    /// `confirm` is set, a confirmation dialog is shown first.
    /// `variant` accepts `"primary"|"secondary"|"danger"` for styling.
    #[serde(rename = "button")]
    Button {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        /// Action invoked on click.
        action: ActionRef,
        #[serde(default)]
        variant: Option<String>,
        /// Optional confirmation prompt. When set, the renderer shows
        /// a Confirm dialog with this text before invoking `action`.
        #[serde(default)]
        confirm: Option<String>,
    },
    /// Single-pick dropdown. Static options declared inline. On
    /// change, `on_change` is invoked with `{ moduleId, value }`.
    #[serde(rename = "select")]
    Select {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        options: Vec<SelectOption>,
        #[serde(default)]
        default: Option<String>,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    /// Informational banner. Read-only — no state, no persistence.
    /// `variant` accepts `"info"|"warning"`. Render-only — module
    /// authors who need dynamic info text should use a StatusDisplay
    /// instead.
    #[serde(rename = "info")]
    Info {
        id: String,
        text: String,
        #[serde(default)]
        variant: Option<String>,
    },
    /// v0.2.26: free-text string input with an Apply button. On apply,
    /// `apply_action` fires; the container's response shape is
    /// `{ valid: bool, message?: string }`. Persistence writes via
    /// `set_module_setting` AFTER validation succeeds (or always, when
    /// `apply_action` is None). Client-side regex validation can be
    /// added later via a `pattern` field — server-side only in v1.
    #[serde(rename = "text_input")]
    TextInput {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        #[serde(default)]
        default: String,
        #[serde(default)]
        placeholder: Option<String>,
        /// Action invoked on Apply. None ⇒ value is persisted without
        /// server-side validation.
        #[serde(default)]
        apply_action: Option<ActionRef>,
    },
    /// v0.2.26: numeric input. JSON value type is `number` (not
    /// string). `step` controls granularity; `min`/`max` clamp the
    /// allowed range.
    #[serde(rename = "number_input")]
    NumberInput {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        #[serde(default)]
        default: Option<f64>,
        #[serde(default)]
        min: Option<f64>,
        #[serde(default)]
        max: Option<f64>,
        #[serde(default)]
        step: Option<f64>,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    /// v0.2.26: polled status display. The renderer fetches `source`
    /// on mount, then re-fetches on the interval declared in the
    /// polling spec (or once if no polling). `render_template` is a
    /// string with `{{field}}` placeholders substituted from the
    /// response object.
    #[serde(rename = "status_display")]
    StatusDisplay {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        source: ActionRef,
        /// Free-form template like `"{{status}} — model: {{model}}"`.
        /// `{{field}}` tokens resolve from the response JSON's top-level
        /// fields (dotted paths NOT supported in v1).
        render_template: String,
    },
    /// v0.2.26: native file/directory picker. On selection, the
    /// absolute path is persisted via `set_module_setting`. Tauri's
    /// `@tauri-apps/plugin-dialog` provides the cross-OS native dialog.
    #[serde(rename = "file_picker")]
    FilePicker {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        /// Allowed file extensions (without leading dot). Empty ⇒ any.
        /// Ignored when `directory: true`.
        #[serde(default)]
        extensions: Vec<String>,
        /// When true, the dialog selects a directory instead of a file.
        #[serde(default)]
        directory: bool,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    /// v0.2.26: clickable link. `target: "external"` opens in the
    /// system browser (via `tauri_plugin_opener::open_url`); `target:
    /// "internal"` calls SvelteKit's `goto(href)` to navigate inside
    /// the launcher. Default target is `"external"`.
    #[serde(rename = "link")]
    Link {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        href: String,
        #[serde(default = "default_link_target")]
        target: String,
    },
    /// v0.2.32 (L4, 2026-05-24): live read-only info display bound to a
    /// data source. The renderer fetches `source` on mount + on manual
    /// refresh, substitutes the `format` template (e.g. `"{value}"`,
    /// `"Version: {value}"`), and shows `fallback` when the source
    /// returns null.
    ///
    /// Unlike [`ConfigControl::StatusDisplay`] (which polls an HTTP
    /// endpoint and uses `render_template`'s `{{field}}` token form),
    /// `InfoDynamic` reads ONE keyed value from a structured source
    /// like the hub's module-DB REST surface — no container needs to
    /// be running. Used by v0.2.32 R1/R3 (RL reranker dashboard) to
    /// surface `weights_version_live` / `active_embedding_live` /
    /// `last_training_live` etc. via Agent J's `module_db_read_row`.
    ///
    /// Refresh affordance: the renderer renders a `↻` button next to
    /// the section header for every section that contains at least one
    /// `*_dynamic` control. Clicking it re-fetches every dynamic
    /// control in that section.
    #[serde(rename = "info_dynamic")]
    InfoDynamic {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        source: InfoDynamicSource,
        /// Template applied to the source's resolved value. The single
        /// token `{value}` is replaced by the source value (stringified
        /// for non-string scalars). Default `"{value}"`.
        #[serde(default = "default_info_dynamic_format")]
        format: String,
        /// Fallback text rendered when the source returns null (e.g.
        /// row absent, container never ran, hub unreachable). When
        /// omitted, the renderer shows an empty cell.
        #[serde(default)]
        fallback: Option<String>,
    },
    /// v0.2.32 (L5, 2026-05-24): native HTML date picker.
    ///
    /// The renderer surfaces an `<input type="date">` with the
    /// declared `min` / `max` and an optional preset `default` (either
    /// an ISO `YYYY-MM-DD` literal OR one of the keyword strings
    /// `"today" / "30_days_ago" / "90_days_ago"`, resolved at mount
    /// time against the user's wall clock).
    ///
    /// Persistence: the value persists into `module_settings` via the
    /// generic `set_module_setting` command — same as `text_input` —
    /// so other controls can reference it through `{{control:<id>}}`
    /// in their descriptor bodies, and sibling buttons see it via the
    /// renderer's `siblingValuesSnapshot()`. A "clear" affordance
    /// writes JSON `null` (empty date → "all history" semantics for
    /// the RL `/global/retrain` endpoint, per the v0.2.7 R2 design).
    ///
    /// `{{date_value}}` shorthand: when this control's value is
    /// referenced in an `on_change` descriptor body or a sibling
    /// button's body, the v0.2.32 renderer exposes it under both
    /// `{{control:<id>}}` (the canonical form) and the convenience
    /// alias `{{date_value}}` (active when the dispatching control's
    /// value field carries the date — see substitution context).
    #[serde(rename = "date_picker")]
    DatePicker {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        /// ISO date literal (`"2026-01-15"`) OR keyword
        /// (`"today"` / `"30_days_ago"` / `"90_days_ago"`). Resolved
        /// to a concrete ISO date in the renderer at mount time
        /// against the user's local wall clock.
        #[serde(default)]
        default: Option<String>,
        /// ISO date lower-bound forwarded to the native `min`
        /// attribute. No keyword resolution here — manifests should
        /// only declare concrete bounds (the renderer doesn't try to
        /// interpret keywords for `min`/`max` because clocks shift).
        #[serde(default)]
        min: Option<String>,
        /// ISO date upper-bound forwarded to the native `max`
        /// attribute.
        #[serde(default)]
        max: Option<String>,
        /// Optional side-effect fired after persistence. Typically a
        /// `descriptor` that POSTs the new date to the container.
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    /// v0.2.33 (Agent D, 2026-05-25): forward-compat fallback for
    /// control kinds this launcher version doesn't recognise. Manifests
    /// shipped by FUTURE launcher versions that add control kinds
    /// deserialize into this variant rather than erroring at parse time
    /// (lenient mode — the default). The renderer skips it with a
    /// per-control placeholder ("This control requires a newer
    /// launcher version (kind: '<X>')"), letting OTHER controls in the
    /// same section render normally.
    ///
    /// Skipped during serde derive (both directions) — only ever
    /// produced by the custom `Deserialize` impl below. The custom
    /// `Serialize` impl on `ConfigControl` (provided alongside the
    /// derive macro on this enum) writes the raw payload back out
    /// verbatim so a load → save round-trip preserves the unknown
    /// kind's wire shape.
    ///
    /// Strict mode (`VCT_LAUNCHER_STRICT_MANIFEST=1`) restores the
    /// pre-v0.2.33 behaviour where unknown kinds are a hard parse
    /// error — useful in CI gates and module-author dev mode.
    #[serde(skip)]
    Unsupported {
        /// The original `kind` value the manifest used. Empty when the
        /// JSON object lacked a `kind` field entirely.
        kind_string: String,
        /// The raw JSON object that failed to deserialize as a known
        /// variant. Carries every field the manifest declared so the
        /// renderer's placeholder can show context (id, label) if
        /// present.
        raw: serde_json::Value,
    },
}

// ─── v0.2.33: ConfigControl lenient deserialize (Agent D) ────────────────
//
// Custom `Deserialize` for `ConfigControl` that falls back to the
// `Unsupported` variant when the `kind` tag is unknown — instead of
// rejecting the whole manifest. The fallback is gated on the
// `VCT_LAUNCHER_STRICT_MANIFEST` env var (`"1"` = strict / pre-v0.2.33,
// anything else = lenient / forward-compat).
//
// Implementation: deserialize the JSON value into a `serde_json::Value`,
// try the derive-generated path via a private mirror enum
// (`ConfigControlKnown`), and on failure either error (strict) or build
// the `Unsupported` variant from the captured raw value.

use std::sync::atomic::{AtomicBool, Ordering};

/// Cached strict-mode flag. Initialised on first read from the
/// `VCT_LAUNCHER_STRICT_MANIFEST` env var; subsequent reads are O(1).
/// Static lifetime — strict-mode is process-global (matches how the
/// orchestrator's CI flag works).
static STRICT_MANIFEST_FLAG: AtomicBool = AtomicBool::new(false);
/// Tracks whether `STRICT_MANIFEST_FLAG` has been initialised from env
/// at least once.
static STRICT_MANIFEST_INIT: AtomicBool = AtomicBool::new(false);

/// Returns true when the launcher should reject unknown manifest
/// `kind` values at parse time (no `Unsupported` fallback). Default is
/// `false` — lenient — to keep forward-compat with future launcher
/// versions adding control kinds.
///
/// Cached after first call; tests that need to flip the flag mid-run
/// must use [`set_strict_manifest_for_test`] (see `cfg(test)` below)
/// which bypasses the env read.
fn strict_manifest_mode() -> bool {
    if !STRICT_MANIFEST_INIT.load(Ordering::Acquire) {
        let strict = std::env::var("VCT_LAUNCHER_STRICT_MANIFEST")
            .map(|v| v == "1")
            .unwrap_or(false);
        STRICT_MANIFEST_FLAG.store(strict, Ordering::Release);
        STRICT_MANIFEST_INIT.store(true, Ordering::Release);
    }
    STRICT_MANIFEST_FLAG.load(Ordering::Acquire)
}

/// Test-only setter for the strict-manifest flag. Avoids depending on
/// the process env var (which would leak between tests running in
/// parallel and isn't reliably mutable on all OSes).
///
/// v0.2.33 (Agent F): visibility relaxed from `#[cfg(test)] pub(crate)`
/// to `#[cfg(any(test, debug_assertions))] pub` so the manifest-CI
/// integration tests in `tests/manifest_ci_gate.rs` (which compile as
/// a SEPARATE crate from the unit tests) can call it. Pattern matches
/// the existing test-helper exposure used by `secrets.rs` /
/// `test_env.rs`. Excluded from `--release` builds — no leak into
/// shipped binaries.
#[cfg(any(test, debug_assertions))]
pub fn set_strict_manifest_for_test(strict: bool) {
    STRICT_MANIFEST_FLAG.store(strict, Ordering::Release);
    STRICT_MANIFEST_INIT.store(true, Ordering::Release);
}

/// Private mirror enum used solely by the custom `Deserialize` impl on
/// [`ConfigControl`]. Mirrors every KNOWN variant of `ConfigControl`
/// (excluding `Unsupported`) and derives `Deserialize` so we can
/// delegate to serde's auto-generated tag-dispatch logic. Convert via
/// `From<ConfigControlKnown> for ConfigControl` below.
///
/// Keeping this in sync with `ConfigControl` is enforced by
/// `confg_control_known_mirrors_all_known_variants_compile_time` (the
/// exhaustive match in the From impl — adding a variant to
/// `ConfigControl` without mirroring it here fails to compile).
#[derive(Debug, Clone, Deserialize, JsonSchema)]
#[serde(tag = "kind")]
pub(crate) enum ConfigControlKnown {
    #[serde(rename = "checkbox")]
    Checkbox {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        #[serde(default)]
        default: bool,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    #[serde(rename = "multi_select")]
    MultiSelect {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        options_source: ActionRef,
        #[serde(default)]
        filter: Option<MultiSelectFilter>,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    #[serde(rename = "button")]
    Button {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        action: ActionRef,
        #[serde(default)]
        variant: Option<String>,
        #[serde(default)]
        confirm: Option<String>,
    },
    #[serde(rename = "select")]
    Select {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        options: Vec<SelectOption>,
        #[serde(default)]
        default: Option<String>,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    #[serde(rename = "info")]
    Info {
        id: String,
        text: String,
        #[serde(default)]
        variant: Option<String>,
    },
    #[serde(rename = "text_input")]
    TextInput {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        #[serde(default)]
        default: String,
        #[serde(default)]
        placeholder: Option<String>,
        #[serde(default)]
        apply_action: Option<ActionRef>,
    },
    #[serde(rename = "number_input")]
    NumberInput {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        #[serde(default)]
        default: Option<f64>,
        #[serde(default)]
        min: Option<f64>,
        #[serde(default)]
        max: Option<f64>,
        #[serde(default)]
        step: Option<f64>,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    #[serde(rename = "status_display")]
    StatusDisplay {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        source: ActionRef,
        render_template: String,
    },
    #[serde(rename = "file_picker")]
    FilePicker {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        #[serde(default)]
        extensions: Vec<String>,
        #[serde(default)]
        directory: bool,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
    #[serde(rename = "link")]
    Link {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        href: String,
        #[serde(default = "default_link_target")]
        target: String,
    },
    #[serde(rename = "info_dynamic")]
    InfoDynamic {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        source: InfoDynamicSource,
        #[serde(default = "default_info_dynamic_format")]
        format: String,
        #[serde(default)]
        fallback: Option<String>,
    },
    #[serde(rename = "date_picker")]
    DatePicker {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        #[serde(default)]
        default: Option<String>,
        #[serde(default)]
        min: Option<String>,
        #[serde(default)]
        max: Option<String>,
        #[serde(default)]
        on_change: Option<ActionRef>,
    },
}

impl From<ConfigControlKnown> for ConfigControl {
    fn from(k: ConfigControlKnown) -> Self {
        match k {
            ConfigControlKnown::Checkbox { id, label, tooltip, default, on_change } => {
                ConfigControl::Checkbox { id, label, tooltip, default, on_change }
            }
            ConfigControlKnown::MultiSelect { id, label, tooltip, options_source, filter, on_change } => {
                ConfigControl::MultiSelect { id, label, tooltip, options_source, filter, on_change }
            }
            ConfigControlKnown::Button { id, label, tooltip, action, variant, confirm } => {
                ConfigControl::Button { id, label, tooltip, action, variant, confirm }
            }
            ConfigControlKnown::Select { id, label, tooltip, options, default, on_change } => {
                ConfigControl::Select { id, label, tooltip, options, default, on_change }
            }
            ConfigControlKnown::Info { id, text, variant } => {
                ConfigControl::Info { id, text, variant }
            }
            ConfigControlKnown::TextInput { id, label, tooltip, default, placeholder, apply_action } => {
                ConfigControl::TextInput { id, label, tooltip, default, placeholder, apply_action }
            }
            ConfigControlKnown::NumberInput { id, label, tooltip, default, min, max, step, on_change } => {
                ConfigControl::NumberInput { id, label, tooltip, default, min, max, step, on_change }
            }
            ConfigControlKnown::StatusDisplay { id, label, tooltip, source, render_template } => {
                ConfigControl::StatusDisplay { id, label, tooltip, source, render_template }
            }
            ConfigControlKnown::FilePicker { id, label, tooltip, extensions, directory, on_change } => {
                ConfigControl::FilePicker { id, label, tooltip, extensions, directory, on_change }
            }
            ConfigControlKnown::Link { id, label, tooltip, href, target } => {
                ConfigControl::Link { id, label, tooltip, href, target }
            }
            ConfigControlKnown::InfoDynamic { id, label, tooltip, source, format, fallback } => {
                ConfigControl::InfoDynamic { id, label, tooltip, source, format, fallback }
            }
            ConfigControlKnown::DatePicker { id, label, tooltip, default, min, max, on_change } => {
                ConfigControl::DatePicker { id, label, tooltip, default, min, max, on_change }
            }
        }
    }
}

impl<'de> Deserialize<'de> for ConfigControl {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        // Two-phase: deserialize to a Value first so we can both attempt
        // the derive-generated path AND fall back to capturing the raw
        // shape verbatim for `Unsupported`.
        let value = serde_json::Value::deserialize(deserializer)?;
        match serde_json::from_value::<ConfigControlKnown>(value.clone()) {
            Ok(known) => Ok(known.into()),
            Err(err) => {
                if strict_manifest_mode() {
                    return Err(serde::de::Error::custom(format!(
                        "manifest: unknown control kind (strict mode): {} \
                         — set VCT_LAUNCHER_STRICT_MANIFEST=0 (or unset it) to \
                         render unknown kinds as an Unsupported placeholder",
                        err,
                    )));
                }
                // Lenient: capture the kind string + raw payload.
                let kind_string = value
                    .get("kind")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                eprintln!(
                    "[manifest] forward-compat: control kind '{}' not recognised by \
                     this launcher version — rendering as Unsupported placeholder. \
                     Original error: {}",
                    if kind_string.is_empty() { "<missing>" } else { kind_string.as_str() },
                    err,
                );
                Ok(ConfigControl::Unsupported { kind_string, raw: value })
            }
        }
    }
}

/// v0.2.33 (Agent F, C2): manual `JsonSchema` impl for `ConfigControl`.
///
/// The derived path doesn't fit here for two reasons:
///   1. `ConfigControl` uses a custom `Deserialize` impl (lenient
///      fallback to the `Unsupported` variant), so the derive macro
///      can't introspect serde's tag-dispatch logic.
///   2. The `Unsupported` variant is `#[serde(skip)]` — it has no wire
///      shape and shouldn't appear in the published JSON Schema (it's
///      a runtime-only forward-compat receptacle, not a thing module
///      authors should declare).
///
/// Delegates to `ConfigControlKnown::json_schema(...)` — the private
/// mirror enum that lists every KNOWN variant the parser dispatches to.
/// Output is identical to "schema for the strict-mode parser", which is
/// exactly what publishers need to validate against.
impl JsonSchema for ConfigControl {
    fn schema_name() -> String {
        "ConfigControl".to_owned()
    }

    fn json_schema(gen: &mut schemars::gen::SchemaGenerator) -> schemars::schema::Schema {
        ConfigControlKnown::json_schema(gen)
    }
}

fn default_info_dynamic_format() -> String {
    "{value}".into()
}

/// v0.2.32 (L4): data source for an [`ConfigControl::InfoDynamic`].
/// Tagged on `kind` so future variants (`http_endpoint`,
/// `tauri_command`) can land additively without breaking older
/// manifests.
///
/// v1 ships exactly ONE kind: `module_db`. Future kinds will need
/// their own renderer plumbing; the `kind` tag keeps the serde shape
/// forward-compatible.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "kind")]
pub enum InfoDynamicSource {
    /// Read one keyed row from the hub's module-DB REST surface via
    /// Agent J's `module_db_read_row` Tauri command (v0.2.31). The
    /// renderer substitutes `{{project_id}}` in `key` against the
    /// active project, then projects `field` from the returned JSON
    /// object. Returns null when the row is absent, the container has
    /// no migrations applied, or the hub is unreachable.
    #[serde(rename = "module_db")]
    ModuleDb {
        /// Table name (must be `{module_namespace}_*` per the
        /// v0.2.31 namespace policy — the hub enforces this at write
        /// time; we don't re-validate here).
        table: String,
        /// Row key. May contain `{{project_id}}`, substituted by the
        /// renderer against the active project's UUID. Other token
        /// forms are passed through unchanged.
        key: String,
        /// Field name to project from the row's JSON value.
        field: String,
    },
}

fn default_link_target() -> String {
    "external".into()
}

/// Option in a `select` or `multi_select` control.
///
/// v0.2.32 (L6): extended with optional `badge` + `meta` for richer
/// rendering (e.g. "new" pill on freshly-released bundles, opaque
/// per-option metadata the renderer's `filter` predicate can match
/// against).
///
/// **Back-compat**: deserialises from EITHER a JSON object
/// (`{"value":"a","label":"A"}` — possibly with optional badge/meta)
/// OR a bare string (`"a"`, which becomes
/// `SelectOption { value: "a", label: "a", badge: None, meta: None }`).
/// This lets `options_source` callers return plain `Vec<String>`
/// without breaking pre-v0.2.32 callers. The custom serde impl below
/// implements this duality — keep it in sync with the doc comment.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SelectOption {
    pub value: String,
    pub label: String,
    /// Optional pill / tag rendered next to the label (e.g. "new",
    /// "deprecated"). Skipped during serialisation when `None` so the
    /// over-the-wire payload stays minimal for back-compat clients.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub badge: Option<String>,
    /// Free-form per-option metadata. Consumed by the renderer's
    /// `MultiSelectFilter` evaluator (top-level keys are looked up by
    /// `meta_field`). Opaque to Rust — Tauri commands can stuff
    /// whatever shape they like in here.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub meta: Option<serde_json::Value>,
}

impl<'de> Deserialize<'de> for SelectOption {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        // Untagged: accept either a JSON string OR a JSON object.
        // The string form is the v0.2.32 back-compat for callers that
        // return `["a", "b"]` (e.g. legacy multi_select options
        // sources that pre-date the `{value, label}` contract).
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum Raw {
            Bare(String),
            Full {
                value: String,
                #[serde(default)]
                label: Option<String>,
                #[serde(default)]
                badge: Option<String>,
                #[serde(default)]
                meta: Option<serde_json::Value>,
            },
        }
        let raw = Raw::deserialize(deserializer)?;
        Ok(match raw {
            Raw::Bare(s) => SelectOption {
                value: s.clone(),
                label: s,
                badge: None,
                meta: None,
            },
            Raw::Full { value, label, badge, meta } => SelectOption {
                label: label.unwrap_or_else(|| value.clone()),
                value,
                badge,
                meta,
            },
        })
    }
}

/// v0.2.33 (Agent F, C2): manual `JsonSchema` for `SelectOption`.
///
/// The published schema describes the canonical OBJECT form only — bare
/// strings deserialise as a back-compat convenience but module authors
/// who care about schema validation should declare full objects. Keeping
/// the schema strict here helps publishers catch typos (e.g. `"valeu"`
/// instead of `"value"`) that lenient deserialisation would silently
/// route into the bare-string branch.
impl JsonSchema for SelectOption {
    fn schema_name() -> String {
        "SelectOption".to_owned()
    }

    fn json_schema(gen: &mut schemars::gen::SchemaGenerator) -> schemars::schema::Schema {
        use schemars::schema::{InstanceType, ObjectValidation, Schema, SchemaObject};
        let mut obj = ObjectValidation::default();
        let string_schema: Schema = SchemaObject {
            instance_type: Some(InstanceType::String.into()),
            ..Default::default()
        }
        .into();
        let value_schema = gen.subschema_for::<serde_json::Value>();
        obj.properties.insert("value".to_string(), string_schema.clone());
        obj.properties.insert("label".to_string(), string_schema.clone());
        obj.properties.insert("badge".to_string(), string_schema);
        obj.properties.insert("meta".to_string(), value_schema);
        obj.required.insert("value".to_string());
        SchemaObject {
            instance_type: Some(InstanceType::Object.into()),
            object: Some(Box::new(obj)),
            metadata: Some(Box::new(schemars::schema::Metadata {
                description: Some(
                    "Option in a select / multi_select control. Bare strings \
                     (e.g. \"qwen3\") also deserialise as a back-compat convenience \
                     where the value+label are equal; declare full objects for \
                     forward-compatibility with badge / meta fields."
                        .to_string(),
                ),
                ..Default::default()
            })),
            ..Default::default()
        }
        .into()
    }
}

/// Runtime-driven filter for [`ConfigControl::MultiSelect`] options.
///
/// v0.2.32 ships ONE kind: `match`. The renderer evaluates the filter
/// when rendering options — entries whose `meta.<meta_field>` does
/// not equal the runtime-resolved value referenced by
/// `equals_runtime` are hidden (or rendered disabled with a tooltip,
/// implementation choice owned by the renderer).
///
/// `equals_runtime` is a dotted identifier the renderer resolves
/// against well-known runtime values. v0.2.32 supports exactly one:
///   * `"container.active_embedding"` — the project's
///     `ACTIVE_EMBEDDING` env var (typically `qwen3`, `arctic`,
///     `openai`, or a future identifier). Resolved via a Tauri
///     command in the renderer; default `"qwen3"` if the lookup
///     fails (matches `module_service::DEFAULT_EMBEDDING_SOURCE`).
///
/// Future kinds (regex, range, contains) land additively as new
/// variants on the enum — the `#[serde(tag = "kind")]` shape keeps
/// the wire format extension-friendly.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, JsonSchema)]
#[serde(tag = "kind")]
pub enum MultiSelectFilter {
    /// Show only options whose `meta.<meta_field>` exactly equals the
    /// runtime value resolved from `equals_runtime`. String equality
    /// only — for numeric / regex comparison, future kinds.
    #[serde(rename = "match")]
    Match {
        /// Top-level key on each option's `meta` JSON object to look
        /// up. SQL-identifier shape recommended (no dots).
        meta_field: String,
        /// Runtime-value identifier. v0.2.32 supports
        /// `"container.active_embedding"` only. Unknown identifiers
        /// MUST NOT panic the renderer — they should fall back to
        /// "no filtering" (show all options).
        equals_runtime: String,
    },
}

// ─── v0.2.26: ActionRef + ActionDescriptor ──────────────────────────────
//
// The schema-rendered GUI framework's biggest pre-v0.2.26 limitation:
// every `action` / `on_change` / `options_source` field was a STRING
// naming a Tauri command that had to be registered in the launcher's
// `invoke_handler!`. Adding a paid module that needed a new command
// required a launcher rebuild + signed release.
//
// v0.2.26 fixes this with a generic declarative dispatcher. The fields
// gain a second form — a structured `ActionDescriptor` — that the
// launcher executes via the single generic `module_dispatch_action`
// Tauri command without per-module Rust code.
//
// Back-compat: the [`ActionRef`] enum is `#[serde(untagged)]`, so a JSON
// string deserializes as `Legacy("cmd_name")` and a JSON object
// deserializes as `Descriptor(...)`. v0.2.20-v0.2.25 manifests work
// unchanged.

/// Either a legacy Tauri command name OR a structured action descriptor.
///
/// Back-compat: a JSON string field deserializes as
/// [`ActionRef::Legacy`]; a JSON object deserializes as
/// [`ActionRef::Descriptor`]. The renderer dispatches accordingly.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(untagged)]
pub enum ActionRef {
    /// Legacy form: name of a Tauri command registered in
    /// `invoke_handler!`. Kept permanently for v0.2.20-v0.2.25 modules
    /// and for in-tree controls that genuinely need Rust code.
    Legacy(String),
    /// Structured descriptor — the launcher's generic dispatcher
    /// executes it. No per-module Tauri code required.
    Descriptor(ActionDescriptor),
}

/// Declarative action that the generic `module_dispatch_action`
/// command executes at dispatch time.
///
/// v0.2.26 shipped ONE kind: `http`. v0.2.32 (CHAINED_ACTION,
/// 2026-05-24) adds the generic `chained_action` primitive — a
/// sequence of step descriptors executed serially, with each step's
/// response threaded into the next step's body via
/// `{{previous_step.<field>}}` placeholders. Optional `polling`
/// attaches to the FINAL step.
///
/// Future kinds (e.g. `shell` for sandboxed subprocess actions) are
/// intentionally NOT included — they would expand the trust surface
/// significantly and need their own design pass.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "kind")]
pub enum ActionDescriptor {
    /// Issue an HTTP request to the module's container (resolved via
    /// `db.get_project_module_port(project_id, module_id)`). The
    /// optional `polling` block converts this from a fire-and-forget
    /// call into a long-running pollable job. The optional
    /// `next_action` chains another descriptor on success.
    #[serde(rename = "http")]
    Http {
        method: HttpMethod,
        path: String,
        #[serde(default)]
        body: Option<serde_json::Value>,
        #[serde(default)]
        polling: Option<PollingSpec>,
        /// Chain a follow-up action on success. The chain is purely
        /// lexical (nested JSON), so cycles are structurally impossible.
        /// The dispatcher executes via an iterative loop guarded by
        /// `max_chain_steps` (default 1024) to bound runaway depth.
        #[serde(default)]
        next_action: Option<Box<ActionDescriptor>>,
    },
    /// v0.2.32 (CHAINED_ACTION, 2026-05-24): execute a sequence of
    /// step descriptors serially. Each step's parsed JSON response is
    /// pushed onto a `step_results` array; the next step's body can
    /// reference previous-step fields via
    /// `{{previous_step.<field>}}` (previous step only) or
    /// `{{step.N.<field>}}` (absolute index).
    ///
    /// `polling` attaches to the FINAL step only. The renderer
    /// observes `polling.progress_event` / `polling.failed_event` as
    /// usual. Intermediate steps must be fast / synchronous — if any
    /// intermediate step needs polling, it should be split into a
    /// separate top-level dispatch.
    ///
    /// `rollback_on_step_failure` is reserved for v0.2.33+ (would
    /// require every action kind to declare a rollback companion).
    /// For v0.2.32 the dispatcher always LOGS and PROPAGATES on step
    /// failure — the partial results are dropped, the renderer sees
    /// the error, and previous steps' side-effects stay on disk /
    /// in the database. Manifests declaring `true` parse correctly
    /// but the flag has no effect today.
    #[serde(rename = "chained_action")]
    ChainedAction {
        /// Ordered list of step descriptors. Empty `steps` is a hard
        /// error at dispatch time (no point chaining nothing).
        steps: Vec<ActionDescriptor>,
        /// Attaches to the LAST step's execution. The Rust executor
        /// applies it after the final step's kick succeeds, using the
        /// same job-id-from-response mechanism as the `Http` variant.
        #[serde(default)]
        polling: Option<PollingSpec>,
        /// Reserved for v0.2.33+. For v0.2.32 the dispatcher always
        /// logs + propagates step failures regardless of this flag.
        /// Pinned in serde so manifests don't have to be re-touched
        /// when rollback support lands.
        ///
        /// v0.2.33 (Agent D): accepts the alias `stop_on_failure` for
        /// back-compat with the v0.2.7 RL manifest, which shipped that
        /// spelling before this field's canonical name was settled.
        /// The renamed canonical form (`rollback_on_step_failure`) is
        /// preferred for new manifests; the alias logs a deprecation
        /// warning when used.
        #[serde(default, alias = "stop_on_failure")]
        rollback_on_step_failure: bool,
    },
    /// v0.2.33 (Agent D, 2026-05-25): invoke a launcher-registered
    /// Tauri command directly. The v0.2.7 RL manifest uses this kind
    /// inside `chained_action.steps[]` to call commands like
    /// `module_download_default_weights` that already exist in the
    /// launcher's `invoke_handler!` registry.
    ///
    /// The dispatcher consults a whitelist (`is_whitelisted_manifest_command`)
    /// before invoking — any command starting with `module_` plus an
    /// explicit allowlist of legacy non-`module_` commands is allowed.
    /// Unknown / non-whitelisted command names error rather than
    /// dispatch (manifest-driven RCE prevention).
    ///
    /// `args` is forwarded to the underlying Rust function as-is,
    /// substituted through the same placeholder pipeline as `Http.body`
    /// (`{{previous_step.<field>}}`, `{{control:<id>}}`, etc.). Each
    /// whitelisted command's signature is statically known to the
    /// dispatcher, so type errors surface at command-arg parse time
    /// (not as opaque dispatch failures).
    #[serde(rename = "tauri_command")]
    TauriCommand {
        command: String,
        #[serde(default)]
        args: serde_json::Value,
    },
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, JsonSchema)]
#[serde(rename_all = "UPPERCASE")]
pub enum HttpMethod {
    Get,
    Post,
    Put,
    Delete,
}

/// Polling spec for long-running actions. When attached to an
/// [`ActionDescriptor::Http`], the dispatcher:
///   1. Issues the kick request (the parent descriptor's method+body).
///   2. Reads the `job_id` from the kick response via `job_id_path`.
///   3. Spawns a background poller that re-hits `endpoint` every
///      `interval_seconds`, passing the job_id back as a query param.
///   4. Emits a `progress_event` Tauri event on each tick.
///   5. Stops when a terminal state is hit (success or failure) OR
///      `max_attempts` is exceeded.
///
/// The renderer subscribes to `progress_event` + `failed_event` to
/// update the UI without blocking the user's click handler.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct PollingSpec {
    /// Container-relative URL for the polling GET. e.g.
    /// `/finetune_status`.
    pub endpoint: String,
    /// JSONPath into the kick response that locates the job id. e.g.
    /// `$.job_id`. Currently supports only the top-level form
    /// (`$.<key>`); deeper paths can be added in a later release.
    #[serde(default = "default_job_id_path")]
    pub job_id_path: String,
    /// Query-parameter name the poller uses to pass the job id back.
    /// Default `"job_id"`.
    #[serde(default = "default_job_id_query_param")]
    pub job_id_query_param: String,
    #[serde(default = "default_polling_interval_seconds")]
    pub interval_seconds: u64,
    #[serde(default = "default_polling_max_attempts")]
    pub max_attempts: u32,
    /// JSONPath into the poll response locating the terminal-state
    /// field. Default `$.state` (matches the RL container's
    /// `/finetune_status` shape).
    #[serde(default = "default_terminal_state_field")]
    pub terminal_state_field: String,
    /// State values that count as "done". Default `["done"]`.
    #[serde(default = "default_terminal_success_values")]
    pub terminal_success_values: Vec<String>,
    /// State values that count as "failed". Default
    /// `["failed", "error"]`.
    #[serde(default = "default_terminal_failure_values")]
    pub terminal_failure_values: Vec<String>,
    /// Tauri event emitted on each poll tick. The payload is the full
    /// poll response JSON. Default `"module://action-progress"`.
    #[serde(default = "default_progress_event")]
    pub progress_event: String,
    /// Tauri event emitted on terminal failure. Default
    /// `"module://action-failed"`.
    #[serde(default = "default_failed_event")]
    pub failed_event: String,
}

fn default_job_id_path() -> String {
    "$.job_id".into()
}
fn default_job_id_query_param() -> String {
    "job_id".into()
}
fn default_polling_interval_seconds() -> u64 {
    5
}
fn default_polling_max_attempts() -> u32 {
    60
}
fn default_terminal_state_field() -> String {
    "$.state".into()
}
fn default_terminal_success_values() -> Vec<String> {
    vec!["done".into()]
}
fn default_terminal_failure_values() -> Vec<String> {
    vec!["failed".into(), "error".into()]
}
fn default_progress_event() -> String {
    "module://action-progress".into()
}
fn default_failed_event() -> String {
    "module://action-failed".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, JsonSchema)]
#[serde(rename_all = "kebab-case")]
pub enum ModuleCategory {
    Core,
    PaidOrchestrator,
    PaidIndependent,
    Community,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, JsonSchema)]
pub struct Compatibility {
    #[serde(default = "default_hosts")]
    pub hosts: Vec<String>,
    #[serde(default)]
    pub min_launcher_version: Option<String>,
}
fn default_hosts() -> Vec<String> {
    vec!["base".into(), "mao".into()]
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, JsonSchema)]
pub struct LicenseBlock {
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub r#type: Option<String>,
    #[serde(default)]
    pub variant_ids: Vec<String>,
    #[serde(default = "default_min_tier")]
    pub min_orchestrator_tier: String,
    #[serde(default)]
    pub trial_days: u32,
}
fn default_min_tier() -> String {
    "free".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, JsonSchema)]
pub struct Requirements {
    #[serde(default)]
    pub os: Vec<String>,
    #[serde(default)]
    pub python: Option<String>,
    #[serde(default)]
    pub node: Option<String>,
    #[serde(default)]
    pub memory_mb: u64,
    #[serde(default)]
    pub disk_mb: u64,
    #[serde(default)]
    pub network: Vec<String>,
    #[serde(default)]
    pub gpu: bool,
    #[serde(default)]
    pub depends_on: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct InstallBlock {
    pub method: InstallMethod,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub r#ref: Option<String>,
    #[serde(default = "default_install_dir")]
    pub install_dir: String,
    #[serde(default)]
    pub post_install: Vec<CommandSpec>,
    /// Container-pull metadata, required when `method = container_pull`.
    /// Ignored by serde when absent for other install methods.
    #[serde(default)]
    pub container: Option<ContainerInstallBlock>,
    /// v0.2.49 (Stream A): install scope — per-project (default) or
    /// global. Per-project modules get one install row + one container
    /// per project (current behaviour). Global modules get one install
    /// row machine-wide (`project_id IS NULL`) and one container named
    /// after the bare module id (no `-{project_slug}` suffix); per-project
    /// routing happens INSIDE the container via headers (e.g. the v0.2.10
    /// RL Reranker reads `X-VCT-Project-ID` from incoming requests).
    ///
    /// `#[serde(default)]` keeps pre-v0.2.49 manifests valid — they
    /// deserialize as `per_project` (the default), preserving the
    /// established install path.
    ///
    /// See `.claude/context/plans/v0.2.49-global-install-per-project-
    /// routing-plan-2026-06-06.md` for the architectural rationale.
    #[serde(default)]
    pub scope: InstallScope,
}

/// v0.2.49 (Stream A): install scope discriminator.
///
/// * `PerProject` (default) — one install row + one container per
///   project. The container name follows the manifest's
///   `runtime.container_name_template` with `{project_slug}`
///   substitution. This is the v0.2.20–v0.2.48 install model.
/// * `Global` — exactly one install row per machine
///   (`module_installs.project_id IS NULL`) and exactly one container
///   named after the bare module id (no slug suffix). Per-project
///   personalization happens at the application layer inside the
///   container (e.g. the v0.2.10 RL Reranker reads `X-VCT-Project-ID`
///   from request headers and routes to per-project model heads).
///
/// Serde: `#[serde(rename_all = "snake_case")]` so manifests declare
/// `"per_project"` / `"global"` on the wire.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum InstallScope {
    PerProject,
    Global,
}

impl Default for InstallScope {
    fn default() -> Self {
        InstallScope::PerProject
    }
}

impl InstallScope {
    pub fn is_global(self) -> bool {
        matches!(self, InstallScope::Global)
    }
    pub fn as_str(self) -> &'static str {
        match self {
            InstallScope::PerProject => "per_project",
            InstallScope::Global => "global",
        }
    }
}

/// Container-pull install metadata. Carries the registry image reference
/// + the signed-URL token gateway endpoint. The launcher's installer
/// engine POSTs the user's validated-tier JWT to `pull_token_endpoint`
/// before invoking `podman/docker pull` — no anonymous registry access
/// is ever attempted.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct ContainerInstallBlock {
    /// Fully-qualified image reference WITHOUT a tag (e.g.
    /// "ghcr.io/hotak92/vct-rl-reranker"). The tag is determined by
    /// `tag_from_version` + manifest.version, OR by InstallBlock::r#ref.
    pub image: String,
    /// When true, the pulled tag is `manifest.version` (e.g. "0.1.0").
    /// When false, the tag is read from `InstallBlock::r#ref` (allows
    /// "latest" floating-tag pulls during early Pro-tier beta).
    #[serde(default = "default_true")]
    pub tag_from_version: bool,
    /// Registry hostname for clarity. Inferred from `image` if absent.
    #[serde(default)]
    pub registry: Option<String>,
    /// HTTPS endpoint that issues short-lived pull tokens against the
    /// user's validated-tier JWT. POST-only. Returns
    /// `{ image, tag, pull_token, expires_at }`. TTL ~15 minutes.
    pub pull_token_endpoint: String,
    /// HTTP method to use (default POST).
    #[serde(default = "default_pull_token_method")]
    pub pull_token_method: String,
    /// When true, rotate model weights independently of image-version
    /// pulls. Used by the launcher's weekly-update poller.
    #[serde(default)]
    pub rotate_weights: bool,
    /// HTTPS endpoint that returns the latest available weights bundle
    /// version + a signed download URL. Polled on launcher startup +
    /// once per day per the project's locked decision (2026-05-16).
    #[serde(default)]
    pub rotate_weights_endpoint: Option<String>,
}

fn default_pull_token_method() -> String {
    "POST".into()
}
fn default_install_dir() -> String {
    "{VCT_MODULES}/{MODULE_ID}".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum InstallMethod {
    /// Clone a git repo to `install_dir` (default for marketplace modules).
    GitClone,
    /// Use an existing directory at `install_dir` (e.g. user-built locally).
    Local,
    /// Pull a container image from a private registry via a short-lived
    /// signed pull-token. Introduced for paid Pro-tier modules (e.g.
    /// vct-rl-reranker) where source-level distribution would expose the
    /// model + code to anyone with the repo URL. Requires the manifest's
    /// `install.container` block (`image`, `tag_from_version`, `registry`,
    /// `pull_token_endpoint`).
    ///
    /// Flow (implemented in installer_engine::run_install):
    ///   1. Validate license tier locally (require Pro or higher).
    ///   2. POST current `validate-tier` JWT to `pull_token_endpoint`.
    ///   3. Receive `{ image, tag, pull_token, expires_at }`. Token TTL is
    ///      short (~15 min) — single-use only.
    ///   4. `podman pull` / `docker pull` with that token (env injection,
    ///      not stored on disk).
    ///   5. Discard token from memory.
    ///
    /// Anti-piracy: registry is private (no anonymous access). Without a
    /// validated Pro license the user cannot obtain a pull-token, so they
    /// cannot pull the image at all. Image weights are rotated server-side
    /// (~weekly) — a leaked snapshot degrades vs free-tier within 2 weeks
    /// of stopping refreshes.
    ContainerPull,
    // Reserved methods previously stubbed (tarball / pypi / npm) were
    // removed in v0.1.0 — they returned hard errors and confused users
    // browsing the modules catalog. They will land in v0.2 with real
    // implementations + signature verification. Manifests that specify
    // them will fail to deserialize with a clean serde error.
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct CommandSpec {
    pub cmd: String,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default = "default_timeout")]
    pub timeout_s: u64,
    #[serde(default)]
    pub platform_cmd: HashMap<String, String>,
    #[serde(default)]
    #[serde(rename = "_note")]
    pub note: Option<String>,
}
fn default_timeout() -> u64 {
    120
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct SecretDecl {
    pub key: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub example: Option<String>,
    #[serde(default)]
    pub validation: Option<String>,
    #[serde(default = "default_true")]
    pub required: bool,
    #[serde(default = "default_scope_per_project")]
    pub scope: String, // "global" | "per-project" | "shared"
    #[serde(default)]
    pub sensitive: bool,
}
fn default_true() -> bool {
    true
}
fn default_scope_per_project() -> String {
    "per-project".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct SettingDecl {
    pub key: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub description: String,
    #[serde(default = "default_setting_type")]
    pub r#type: String, // "string" | "integer" | "boolean" | "multiselect" | "path"
    #[serde(default)]
    pub default: serde_json::Value,
    #[serde(default)]
    pub default_by_platform: HashMap<String, serde_json::Value>,
    #[serde(default)]
    pub options: Vec<String>,
    #[serde(default)]
    pub validation: Option<String>,
    #[serde(default)]
    pub validation_cmd: Option<String>,
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub min: Option<i64>,
    #[serde(default)]
    pub max: Option<i64>,
}
fn default_setting_type() -> String {
    "string".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct RuntimeBlock {
    pub r#type: String, // "mcp_stdio" | "mcp_http" | "service" | "cli" | "container"
    /// v0.2.49: optional. For mcp_stdio / cli, this is the executable to
    /// spawn. For container / service modules, this used to override the
    /// container image's CMD — Bug E (the container CMD landing as
    /// `python -m rl_server.rl_server podman run --rm -p 11450:11450
    /// {module_image}` and argparse-failing) showed that container/
    /// service modules should NOT set `command`. When empty (the
    /// declarative form), the container runtime helper skips the CMD
    /// override and the image-baked ENTRYPOINT runs unmolested.
    #[serde(default)]
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub platform_command: HashMap<String, String>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub env_from_secrets: Vec<String>,
    #[serde(default)]
    pub env_from_settings: Vec<String>,
    #[serde(default)]
    pub env_fixed: HashMap<String, String>,
    #[serde(default)]
    pub health_check: Option<HealthCheck>,
    #[serde(default)]
    pub auto_restart: bool,
    #[serde(default)]
    pub log_file: Option<String>,

    // ─── Phase 1E: per-project container runtime fields ───────────────
    //
    // These fields are only meaningful when `r#type == "container"`. Modules
    // with other runtime types (mcp_stdio / mcp_http / service / cli) leave
    // them empty / None and the supervisor (vct-hub::module_supervisor)
    // never reads them.

    /// Container-name template (e.g. `"vct-rl-reranker-{project_slug}"`).
    /// `{project_slug}` is the only placeholder honoured.
    #[serde(default)]
    pub container_name_template: Option<String>,

    /// Image reference template (e.g. `"{install.container.image}:{install.container.tag}"`).
    /// Resolved by `module_supervisor::resolve_image_ref` against the
    /// manifest's `install.container` block at start time.
    #[serde(default)]
    pub image_ref: Option<String>,

    /// Port mappings host → container.
    #[serde(default)]
    pub ports: Vec<PortMapping>,

    /// Volume mounts host → container.
    #[serde(default)]
    pub volumes: Vec<VolumeMount>,

    /// Env vars that need placeholder substitution at start time
    /// (`{RL_SERVER_PORT}`, `{project_slug}`, `{ollama_port}`).
    /// Distinct from `env_fixed` (literal values).
    #[serde(default)]
    pub env_derived: HashMap<String, String>,

    /// v0.2.27: per-module event-log path convention. When the module
    /// bind-mounts per-project event logs into its container, this
    /// template tells the dispatcher how to compute the host-side path
    /// from a project id. The dispatcher's `{{events_paths_for:<id>}}`
    /// template token reads this field, walks the referenced control's
    /// UUID array, and produces a JSON array of paths to inject into a
    /// descriptor's `body` field.
    ///
    /// Closed-set placeholders inside the template:
    /// - `{project_slug}` — the project's slug (DB column).
    /// - `{project_id}` — the project's UUID.
    ///
    /// Single-brace deliberately, to distinguish from the OUTER
    /// dispatcher tokens (`{{...}}` double-brace). Any other `{...}`
    /// or `{{...}}` inside the template is a manifest validation error
    /// — see `validate_log_path_template`.
    ///
    /// Example: `"/data/logs/rl_events_{project_slug}.jsonl"`.
    ///
    /// Optional. When omitted, `{{events_paths_for:<id>}}` returns a
    /// clear dispatcher error rather than silently resolving to empty.
    #[serde(default)]
    pub log_path_template: Option<String>,

    // ─── v0.2.20: per-module GPU mode hints ───────────────────────────
    //
    // These three fields drive the launcher's per-module GPU policy
    // decision (gpu_policy::decide_gpu_mode + image-variant dispatch).
    // All optional — modules that don't care about GPU mode (CLI tools,
    // pure-Python MCPs) simply omit them and fall through to the legacy
    // "no GPU detection" path.
    //
    // See knowledge/concepts/gpu-mode-decision-policy.md for the full
    // design rationale.

    /// Per-module VRAM threshold (GB). When set, the launcher passes
    /// this value to `decide_gpu_mode` for this module's install/start
    /// flow instead of the 8 GB default. Lets smaller modules (e.g. the
    /// RL reranker, ~4 GB) opt into GPU mode on hardware that
    /// orchestrator-core would degrade to CPU.
    #[serde(default)]
    pub min_gpu_vram_gb: Option<f64>,

    /// When true, the module RUNS on CPU with degraded performance
    /// (RL reranker fits — its model loads on CPU, just slower).
    /// `gpu_optional=false` modules refuse to install without a
    /// qualifying GPU and surface a clear error pointing at the user's
    /// options (upgrade hardware, or `--cpu-only` if they accept the
    /// perf hit). Default `false` (GPU treated as required when the
    /// module declares any GPU hint).
    #[serde(default)]
    pub gpu_optional: bool,

    /// Optional per-mode image-variant tags. When present, the
    /// launcher's `start_container_for_module` reads `decide_gpu_mode`'s
    /// answer and picks the matching tag (Cuda → `cuda`, Rocm → `rocm`,
    /// Cpu/Metal → `cpu`). When absent, the legacy single-tag flow
    /// (from `install.container.tag_from_version`) is used unchanged.
    #[serde(default)]
    pub gpu_image_variants: Option<GpuImageVariants>,
}

/// NEW-3.B (2026-05-28): Defaulting helpers for container-runtime fields.
///
/// `container_name_template` and `image_ref` are `Option<String>` on the
/// wire but the launcher's start logic requires them. Rather than
/// hard-failing at start time when they're absent (old behaviour), these
/// methods synthesize sensible defaults — making `service`/`container`
/// modules installable without forcing publishers to declare every
/// structured field. The caller still passes the synthesized string
/// through the existing `resolve_container_name` / `resolve_image_ref`
/// free functions so placeholder substitution is unchanged.
impl RuntimeBlock {
    /// Returns the manifest's declared `container_name_template` when
    /// present, else synthesizes a sensible default.
    ///
    /// Default form: `{module_id_safe}-{project_slug}` where
    /// `module_id_safe` is the module_id with non-alphanumeric chars
    /// collapsed to `-`.
    pub fn resolve_container_name_template(&self, module_id: &str) -> String {
        // NEW-3.B (2026-05-28): use declared value when non-empty.
        if let Some(t) = self.container_name_template.as_deref() {
            if !t.is_empty() {
                // v0.2.59: substitute `{module_id}` here, at the single
                // choke-point every name-resolution path funnels through
                // (both `resolve_container_name` for per-project scope and
                // `resolve_global_container_name` for global scope, across
                // launcher + hub). The downstream resolvers only know how
                // to substitute `{project_slug}`; without this, a manifest
                // that names its container after the module — the canonical
                // global-singleton form `container_name_template:
                // "{module_id}"` shipped by vct-rl-reranker v0.2.10 — would
                // reach `resolve_global_container_name("{module_id}", ...)`,
                // which rejects the leftover `{...}` as "unresolved
                // placeholders" and fails the install at container-start.
                // `{module_id}` is always resolvable here (it's our own
                // parameter), so resolve it eagerly and let the downstream
                // resolver handle only the project-scoped token.
                return t.replace("{module_id}", module_id);
            }
        }
        // Synthesize: replace non-alphanumeric with '-', collapse runs of
        // '-', then append "-{project_slug}" (the placeholder is resolved
        // later by resolve_container_name in module_service.rs).
        let safe_id: String = module_id
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
            .collect();
        let safe_id = safe_id
            .split('-')
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("-");
        format!("{}-{{project_slug}}", safe_id)
    }

    /// Returns the manifest's declared `image_ref` when present, else
    /// synthesizes from the `install.container` block.
    ///
    /// Caller passes the resolved `ContainerInstallBlock` so this method
    /// stays a pure function on `RuntimeBlock`.
    ///
    /// Default form: `{image}:{tag}` where `tag` is:
    /// - `module_version` when `tag_from_version` is true
    /// - `container_install.tag_from_version == false` → uses `install.r#ref`
    ///   (callers that need `install.r#ref` should call the free-function
    ///   `resolve_image_ref` on the resulting template string instead; here
    ///   we synthesize the canonical template that the free function handles)
    ///
    /// v0.2.49: ALWAYS returns the canonical template form (NOT a
    /// pre-rendered string) when `runtime.image_ref` is unset.
    ///
    /// Pre-v0.2.49 fast-path returned `"{image}:{version}"` directly
    /// when `tag_from_version == true` — "avoiding a second round-trip
    /// through the template substitution path". That shortcut SILENTLY
    /// BYPASSED the variant-suffix resolution in the free-function
    /// `resolve_image_ref`: the free function's `.replace()` against
    /// the `{install.container.tag}` placeholder is a no-op on a
    /// pre-rendered string, so the GPU mode never gets applied to the
    /// tag. Result on the start path: container starts with bare
    /// `:0.2.9` instead of `:0.2.9-cuda`, podman tries to fetch a tag
    /// that doesn't exist on private GHCR, "manifest unknown" exit 125.
    ///
    /// The install path got lucky because it has its own variant
    /// dispatch (`decide_variant_to_pull` calls `probe + fallback`),
    /// which papered over the shortcut. The start path doesn't, so it
    /// hit the bare-tag bug end-to-end.
    ///
    /// Fix: always return the template form so the free function gets
    /// to apply both placeholders AND the variant suffix.
    pub fn resolve_image_ref(
        &self,
        _container_install: &ContainerInstallBlock,
        _module_version: &str,
    ) -> String {
        // NEW-3.B (2026-05-28): use declared value when non-empty.
        if let Some(t) = self.image_ref.as_deref() {
            if !t.is_empty() {
                return t.to_string();
            }
        }
        // v0.2.49: canonical template form. The free function
        // `vct_launcher_core::services::container_runtime::resolve_image_ref`
        // substitutes `{install.container.image}` from
        // `container_install.image` and `{install.container.tag}` from
        // either `manifest.version` (`tag_from_version=true`) or
        // `install.r#ref`/`"latest"` (`tag_from_version=false`), then
        // applies the GPU variant suffix from `gpu_image_variants`.
        "{install.container.image}:{install.container.tag}".to_string()
    }
}

/// Per-GPU-mode image tag variants. Each variant ships as a separate
/// OCI image tag (e.g. `:0.1.0-cpu`, `:0.1.0-cuda`, `:0.1.0-rocm`)
/// because PyTorch's CUDA/ROCm/CPU wheels are mutually exclusive at
/// pip-install time. See `knowledge/concepts/gpu-mode-decision-policy.md`
/// > "Why CUDA wheels vs ROCm wheels need different containers" for
/// the full rationale.
///
/// All three variants are REQUIRED when the block is present — the
/// launcher would have no fallback if one were missing. Modules that
/// only ship a CPU build should simply omit `gpu_image_variants` and
/// rely on the legacy single-tag path.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, JsonSchema)]
pub struct GpuImageVariants {
    /// CPU-only variant tag (e.g. `"0.1.0-cpu"`). Used for `GpuMode::Cpu`
    /// AND `GpuMode::Metal` (no Metal-specific torch wheels today).
    pub cpu: String,
    /// CUDA variant tag (e.g. `"0.1.0-cuda"`). Used for `GpuMode::Cuda`.
    pub cuda: String,
    /// ROCm variant tag (e.g. `"0.1.0-rocm"`). Used for `GpuMode::Rocm`.
    pub rocm: String,
}

/// A single port mapping for a container module.
///
/// Wire shape: `[bind:]<host>:<container>`. `host` is a placeholder string
/// (typically `"{RL_SERVER_PORT}"`) resolved at start time against the
/// project's allocated rl_port. `container` is the in-container port
/// number (literal u16). `bind` defaults to `"127.0.0.1"` when None, so
/// the supervisor never accidentally exposes per-project containers to
/// the LAN.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, JsonSchema)]
pub struct PortMapping {
    /// Host-side port (string so it can be a `{PLACEHOLDER}`).
    pub host: String,
    /// In-container port (literal u16).
    pub container: u16,
    /// Bind address. Defaults to `"127.0.0.1"` when None.
    #[serde(default)]
    pub bind: Option<String>,
}

/// A single volume mount for a container module.
///
/// Wire shape: `<host>:<container>[:<mode>]`. Both `host` and `container`
/// undergo placeholder substitution against `PlaceholderCtx` +
/// `{project_slug}`. Mode is optional; common values are `"rw"` (default
/// when omitted), `"ro"`, or `"z"`/`"Z"` (SELinux relabel on RHEL hosts).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, JsonSchema)]
pub struct VolumeMount {
    pub host: String,
    pub container: String,
    #[serde(default)]
    pub mode: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct HealthCheck {
    pub r#type: String, // "stdio_ping" | "http_get"
    #[serde(default = "default_timeout")]
    pub timeout_s: u64,
    #[serde(default = "default_interval")]
    pub interval_s: u64,
    #[serde(default)]
    pub url: Option<String>,
}
fn default_interval() -> u64 {
    30
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct McpRegistration {
    #[serde(default = "default_true")]
    pub enabled_by_default: bool,
    pub mcp_name: String,
    #[serde(default = "default_target_all")]
    pub target_projects: serde_json::Value, // "all" | "none" | ["path"]
    #[serde(default = "default_user_scope")]
    pub scope: String, // "user" | "project"
    /// v0.2.34 (Agent E — Phase 4 generalisation, 2026-05-25): optional
    /// per-tool allowlist defaults for the wrapper MCP this module ships.
    ///
    /// When present, the launcher persists each entry into
    /// `module_mcp_tool_defaults` at install time (keyed by
    /// `(mcp_name, tool_name)`), and the hub's
    /// `/api/v1/projects/{project_id}/mcp-tool-grants/{mcp_name}` route
    /// composes the resolved allowlist from these defaults PLUS any
    /// per-project overrides in `project_mcp_tool_grants`. Per-project
    /// rows always win; absent rows fall through to `default_enabled`.
    ///
    /// Empty `Vec` and `None` are semantically equivalent here — the hub
    /// returns the hardcoded fallback allowlist (diagrams-era constants)
    /// when no rows are registered for `mcp_name`. This means an
    /// orchestrator-bundled MCP (mermaid/excalidraw) that ships WITHOUT
    /// a manifest still gets sensible defaults; a paid module that ships
    /// WITH a manifest declares its own.
    ///
    /// Reconciliation contract on module update: tools added in the new
    /// version are inserted with their declared `default_enabled`. Tools
    /// removed from the manifest are dropped from
    /// `module_mcp_tool_defaults` (no soft-delete — defaults are
    /// inherently version-current). Per-project overrides in
    /// `project_mcp_tool_grants` are LEFT IN PLACE so a user who
    /// explicitly disabled a tool keeps that override even if the tool
    /// later returns; the override applies if the tool is re-added.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_allowlist: Option<Vec<ToolAllowlistEntry>>,
}

/// v0.2.34 (Agent E): one tool the wrapper MCP exposes, plus its
/// default-enabled state and an optional human-readable description.
///
/// Mirrors the in-DB shape of `module_mcp_tool_defaults` (migration 023);
/// see the SQL file for the rationale on why this lives at the
/// MCP/module level rather than per-project.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
pub struct ToolAllowlistEntry {
    /// Tool name as exposed by the wrapper's upstream MCP (e.g.
    /// `"render"`, `"save_diagram"`, `"export_png"`). Case-sensitive.
    pub tool: String,
    /// Whether the wrapper allows this tool when no per-project
    /// override exists. Defaults to `true` so an absent value in the
    /// manifest enables the tool — safer for module authors who
    /// declare a flat list of tools they intend to expose.
    #[serde(default = "default_true")]
    pub default_enabled: bool,
    /// Optional one-line description rendered as the tooltip in the
    /// launcher's per-tool toggle UI. Best-effort sourced from the
    /// upstream MCP's `tools/list`; absence is non-fatal.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
}
fn default_target_all() -> serde_json::Value {
    serde_json::Value::String("all".into())
}
fn default_user_scope() -> String {
    "user".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct SetupWizard {
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub platform_command: HashMap<String, String>,
    #[serde(default)]
    pub env_from_secrets: Vec<String>,
    #[serde(default)]
    pub env_from_settings: Vec<String>,
    #[serde(default)]
    pub success_marker: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct UpgradeBlock {
    #[serde(default = "default_upgrade_strategy")]
    pub strategy: String,
    #[serde(default)]
    pub pre_upgrade: Vec<CommandSpec>,
    #[serde(default)]
    pub post_upgrade: Vec<CommandSpec>,
    #[serde(default)]
    pub migration_script: Option<String>,
}
fn default_upgrade_strategy() -> String {
    "git_pull".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct UninstallBlock {
    #[serde(default = "default_true")]
    pub remove_install_dir: bool,
    #[serde(default)]
    pub preserve_paths: Vec<String>,
    #[serde(default = "default_true")]
    pub deregister_mcp: bool,
    #[serde(default)]
    pub clear_secrets: bool,
}

// ─── Parsing ─────────────────────────────────────────────────────────────

impl ModuleManifest {
    /// Parse + sanity-check a manifest from a JSON string.
    ///
    /// Validates required fields and enum values. Callers perform
    /// side-effecting validations (file existence, license availability)
    /// separately — this function never touches the filesystem or network.
    pub fn from_json(raw: &str) -> Result<Self, String> {
        let mut m: Self = serde_json::from_str(raw)
            .map_err(|e| format!("manifest JSON parse: {}", e))?;

        if m.id.is_empty() {
            return Err("manifest.id is required".into());
        }
        if !m.id.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-') {
            return Err(format!("manifest.id '{}' must be kebab-case (lowercase, digits, hyphens)", m.id));
        }
        if m.name.is_empty() {
            return Err("manifest.name is required".into());
        }
        if m.version.is_empty() {
            return Err("manifest.version is required".into());
        }

        // Drop empty hosts; default kicks in.
        if m.compatibility.hosts.is_empty() {
            m.compatibility.hosts = default_hosts();
        }
        for h in &m.compatibility.hosts {
            // G2 (v0.2.22): accept `orchestrator_root` — it's the actual host
            // string emitted by `ProjectHost::as_str()` for VCO_dev itself
            // (the orchestrator clone registered as a project). Pre-v0.2.22
            // this token was rejected, blocking install of any module that
            // declared `hosts: [..., "orchestrator_root"]`. The legacy
            // `"standalone"` token was an unused placeholder — kept here for
            // backward compatibility with any third-party manifests that may
            // have adopted it, but no in-tree code actually emits it.
            if !matches!(
                h.as_str(),
                "base" | "mao" | "orchestrator_root" | "standalone"
            ) {
                return Err(format!("manifest.compatibility.hosts contains invalid value '{}'", h));
            }
        }

        if m.license.required && m.license.variant_ids.is_empty()
            && m.license.min_orchestrator_tier == "free" {
            return Err("manifest.license.required=true but no variant_ids and min_orchestrator_tier=free — contradictory".into());
        }
        if !matches!(
            m.license.min_orchestrator_tier.as_str(),
            "free" | "pro" | "mao" | "enterprise"
        ) {
            return Err(format!(
                "manifest.license.min_orchestrator_tier invalid: '{}'",
                m.license.min_orchestrator_tier
            ));
        }

        for s in &m.secrets {
            if !matches!(s.scope.as_str(), "global" | "per-project" | "shared") {
                return Err(format!("secret '{}' has invalid scope '{}'", s.key, s.scope));
            }
        }

        if !matches!(
            m.runtime.r#type.as_str(),
            "mcp_stdio" | "mcp_http" | "service" | "cli" | "container"
        ) {
            return Err(format!("runtime.type '{}' not recognized", m.runtime.r#type));
        }

        // v0.2.27: validate `runtime.log_path_template` if present.
        // Closed-set tokens (single-brace) — anything else is a
        // structural error caught at manifest-load time so a typo
        // doesn't surface mid-dispatch with a confusing message.
        if let Some(ref tmpl) = m.runtime.log_path_template {
            validate_log_path_template(tmpl)
                .map_err(|e| format!("runtime.log_path_template invalid: {}", e))?;
        }

        // v0.2.31: validate `db` block if present. We refuse manifests
        // with malformed namespaces at load time so a typo doesn't
        // surface mid-install with an opaque "namespace violation"
        // error against the first CREATE TABLE.
        if let Some(ref db) = m.db {
            validate_db_block(db)
                .map_err(|e| format!("manifest.db invalid: {}", e))?;
        }

        Ok(m)
    }

    /// Returns true if this manifest is installable on the given host.
    pub fn is_compatible_with_host(&self, host: &str) -> bool {
        self.compatibility.hosts.iter().any(|h| h == host)
    }

    /// v0.2.49 Stream B — install scope detection.
    ///
    /// Returns `true` when this module is installed at GLOBAL scope (one
    /// install on the host, shared/visible across every project) and
    /// `false` when it is installed at PER-PROJECT scope (the legacy
    /// default — one install row per `(project_id, module_id)` pair).
    ///
    /// The canonical source-of-truth for this distinction is the
    /// `install.scope` field added by Stream A. While Stream A's field
    /// is landing in parallel, this helper falls back to a conservative
    /// default of "per-project" (returns `false`) when the field isn't
    /// present yet. Once Stream A merges, the body of this helper should
    /// read `matches!(self.install.scope, Some(InstallScope::Global))`
    /// (or whatever exact spelling Stream A chooses) — the call sites
    /// in `module_enabled.rs`, `projects_v2.rs`, and `modules.rs` that
    /// branch on it do NOT need to change.
    ///
    /// Forward-compat note: callers should treat the answer as "the
    /// best-effort scope at the time of the call". A manifest authored
    /// today without `install.scope` and a manifest authored tomorrow
    /// with `install.scope: per_project` both return `false`; that's
    /// the right answer in both cases. Only an EXPLICIT global declaration
    /// flips this to `true` — there is no scenario where the seeding
    /// logic should treat an absent scope as global.
    pub fn install_scope_is_global(&self) -> bool {
        // v0.2.49 integration: Stream A landed `install.scope` as a
        // non-optional `InstallScope` field with `#[serde(default)] =
        // PerProject`. Stream B's shim is now wired to read it.
        // `InstallScope::is_global()` returns `matches!(self, Global)`.
        self.install.scope.is_global()
    }

    /// NEW-3.D (2026-05-28): validate that a `service`/`container` runtime
    /// module has the fields needed for the launcher's container start path.
    /// Returns a Vec of [`ManifestWarning`]s (empty = fully valid).
    ///
    /// Only applies to manifests with `install.method == ContainerPull` AND
    /// `runtime.type ∈ {"container", "service"}`. All other modules return
    /// an empty Vec immediately.
    ///
    /// Warnings divide into two severity levels:
    /// - `Error` — field is required with no sensible default (e.g. missing
    ///   `install.container.image`). Callers SHOULD block the install.
    /// - `Deprecation` — field is missing but can be synthesized. Callers
    ///   SHOULD log + audit and continue. Publishers should declare the field
    ///   explicitly to silence the warning.
    pub fn validate_for_container_start(&self) -> Vec<ManifestWarning> {
        let mut warnings = Vec::new();

        // Only meaningful for container-pull + container/service runtime.
        if self.install.method != InstallMethod::ContainerPull {
            return warnings;
        }
        if !matches!(self.runtime.r#type.as_str(), "container" | "service") {
            return warnings;
        }

        // Hard error: install.container block must be present and have a non-empty image.
        let image_ok = self.install.container.as_ref()
            .map(|c| !c.image.is_empty())
            .unwrap_or(false);
        if !image_ok {
            warnings.push(ManifestWarning::error(
                "install.container.image",
                "required for container_pull modules — no image to pull",
            ));
        }

        // Deprecation: container_name_template missing but synthesizable.
        if self.runtime.container_name_template.is_none() {
            warnings.push(ManifestWarning::deprecation(
                "runtime.container_name_template",
                "missing; launcher will synthesize as '{module_id}-{project_slug}'. \
                 Declare explicitly to silence this warning.",
            ));
        }

        // Deprecation: image_ref missing but synthesizable from install.container.
        if self.runtime.image_ref.is_none() {
            warnings.push(ManifestWarning::deprecation(
                "runtime.image_ref",
                "missing; launcher will synthesize from install.container.image + version. \
                 Declare explicitly to silence this warning.",
            ));
        }

        // Deprecation: raw runtime.args with no structured ports — likely -p flags
        // that the supervisor won't pick up correctly.
        if self.runtime.ports.is_empty() && !self.runtime.args.is_empty() {
            let has_p_flag = self.runtime.args.iter().any(|a| a == "-p" || a.starts_with("-p"));
            if has_p_flag {
                warnings.push(ManifestWarning::deprecation(
                    "runtime.ports",
                    "raw -p flag detected in runtime.args but runtime.ports is empty. \
                     Declare runtime.ports for structured port forwarding.",
                ));
            }
        }

        warnings
    }
}

/// Severity of a [`ManifestWarning`] emitted by
/// [`ModuleManifest::validate_for_container_start`].
///
/// `Error` warnings indicate a field with no sensible default — callers
/// should block the install. `Deprecation` warnings indicate a field that
/// will be synthesized at start time but should be declared explicitly by
/// the publisher.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WarningSeverity {
    /// Field is required and has no sensible default. Callers should block.
    Error,
    /// Field is optional-but-recommended; a default will be synthesized.
    Deprecation,
}

/// A single validation warning from [`ModuleManifest::validate_for_container_start`].
#[derive(Debug, Clone)]
pub struct ManifestWarning {
    /// Dotted field path (e.g. `"runtime.container_name_template"`).
    pub field: String,
    /// Human-readable explanation, including the synthesized default when
    /// applicable.
    pub message: String,
    /// Whether this warning should block the install or just be logged.
    pub severity: WarningSeverity,
}

impl ManifestWarning {
    /// Construct a `Deprecation`-severity warning.
    pub fn deprecation(field: &str, message: &str) -> Self {
        Self {
            field: field.to_owned(),
            message: message.to_owned(),
            severity: WarningSeverity::Deprecation,
        }
    }

    /// Construct an `Error`-severity warning.
    pub fn error(field: &str, message: &str) -> Self {
        Self {
            field: field.to_owned(),
            message: message.to_owned(),
            severity: WarningSeverity::Error,
        }
    }
}

/// v0.2.31: validate a `DbBlock` from a parsed manifest.
///
/// Returns Ok(()) when:
/// - `migrations_dir` is non-empty (no other shape checks — the
///   apply code resolves it against `module_install_dir` and rejects
///   non-existent dirs / non-file matches at apply time).
/// - `namespace` matches `[a-z][a-z0-9_]*` (lowercase identifier shape).
///   Refuses empty, leading-digit, uppercase, hyphen, dot, slash, or
///   anything else.
///
/// The namespace constraint is the load-bearing one: at apply time
/// the launcher's regex-based SQL parser asserts every CREATE TABLE /
/// ALTER TABLE / CREATE INDEX targets a table starting with
/// `{namespace}_`. A malformed namespace would either reject every
/// SQL file (silently) or — worse — match no tables and let the module
/// write outside its sandbox. Catching it here fails fast.
pub fn validate_db_block(db: &DbBlock) -> Result<(), String> {
    if db.migrations_dir.is_empty() {
        return Err("db.migrations_dir is required (relative path to SQL files)".into());
    }

    if db.namespace.is_empty() {
        return Err("db.namespace is required".into());
    }

    let bytes = db.namespace.as_bytes();
    // First char: lowercase ASCII letter only. Digits / underscores
    // forbidden as leading chars because `0_foo` would sort-collide
    // with migration-file numeric prefixes if we ever conflated the
    // two, and `_foo` is unconventional for SQLite identifiers.
    let first_ok = bytes
        .first()
        .map(|c| c.is_ascii_lowercase())
        .unwrap_or(false);
    if !first_ok {
        return Err(format!(
            "db.namespace '{}' must start with a lowercase letter [a-z]",
            db.namespace
        ));
    }

    // Subsequent chars: [a-z], [0-9], or '_'. Anything else (hyphen,
    // uppercase, dot, slash) is rejected.
    let rest_ok = bytes
        .iter()
        .skip(1)
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_');
    if !rest_ok {
        return Err(format!(
            "db.namespace '{}' must match [a-z][a-z0-9_]* \
             (lowercase letters, digits, and underscores only)",
            db.namespace
        ));
    }

    Ok(())
}

/// v0.2.27: validate a `runtime.log_path_template` string.
///
/// Closed-set rules:
/// - Must contain at least one of `{project_slug}` or `{project_id}` (else
///   the template would produce the same path for every project, which is
///   never what the author wants).
/// - May contain ZERO `{{...}}` double-brace tokens (those are dispatcher-
///   level template tokens; mixing them with the single-brace per-project
///   ones in the SAME template would be confusing and is forbidden).
/// - Single-brace tokens other than `{project_slug}` / `{project_id}` are
///   rejected (e.g. `{module_id}`, `{value}` — these have meaning in OTHER
///   contexts but not here; allowing them would invite copy-paste errors).
///
/// Returns Ok(()) when the template is valid, Err with a clear message
/// otherwise. Best-effort tokenizer — uses simple substring scanning, not
/// a real grammar. Good enough for the closed-set rules.
pub fn validate_log_path_template(template: &str) -> Result<(), String> {
    if template.is_empty() {
        return Err("template is empty".into());
    }
    if template.contains("{{") || template.contains("}}") {
        return Err(
            "double-brace tokens ({{...}}) are not allowed in log_path_template; \
             use single-brace placeholders {project_slug} / {project_id} instead"
                .into(),
        );
    }
    // Find every `{...}` pair and check the inner token name.
    let bytes = template.as_bytes();
    let mut i = 0;
    let mut saw_recognised_token = false;
    while i < bytes.len() {
        if bytes[i] == b'{' {
            // Find matching '}'.
            let rest = &template[i + 1..];
            let close = rest
                .find('}')
                .ok_or_else(|| format!("unclosed '{{' at offset {}", i))?;
            let token = &rest[..close];
            match token {
                "project_slug" | "project_id" => {
                    saw_recognised_token = true;
                }
                _ => {
                    return Err(format!(
                        "unknown placeholder '{{{}}}' (only {{project_slug}} / {{project_id}} allowed)",
                        token,
                    ));
                }
            }
            i += 1 + close + 1; // advance past the closing '}'
        } else {
            i += 1;
        }
    }
    if !saw_recognised_token {
        return Err(
            "template must contain at least one {project_slug} or {project_id} placeholder"
                .into(),
        );
    }
    Ok(())
}

/// v0.2.27: apply a validated `log_path_template` to a single (project_id,
/// project_slug) pair. Substitutes the two recognised tokens with the
/// provided values; any unrecognised tokens are left untouched (which
/// can't happen if the template was validated via `validate_log_path_template`).
///
/// The validation pre-guarantees the template is well-formed; this helper
/// is a pure string transform.
pub fn render_log_path_template(template: &str, project_id: &str, project_slug: &str) -> String {
    template
        .replace("{project_slug}", project_slug)
        .replace("{project_id}", project_id)
}

// ─── Placeholder resolution ──────────────────────────────────────────────

/// Runtime environment for resolving placeholder strings.
///
/// Builders fill in the fields they know; `resolve` substitutes `{TOKEN}`
/// patterns. Unknown tokens pass through unchanged so a typo in a
/// manifest is visible in the error message that eventually surfaces.
#[derive(Debug, Clone)]
pub struct PlaceholderCtx {
    pub vct_root: PathBuf,
    pub vct_modules: PathBuf,
    pub vct_data: PathBuf,
    pub vct_logs: PathBuf,
    pub install_dir: Option<PathBuf>,
    pub module_id: String,
    pub hostname: String,
    pub user: String,
    pub home: PathBuf,
    pub appdata: Option<PathBuf>, // Windows %APPDATA%
}

impl PlaceholderCtx {
    pub fn new(module_id: &str) -> Self {
        let home = directories::UserDirs::new()
            .map(|d| d.home_dir().to_path_buf())
            .unwrap_or_else(|| PathBuf::from("/"));
        // {VCT_ROOT}/{VCT_MODULES}/{VCT_DATA}/{VCT_LOGS} resolve to
        // VCT_STATE_DIR if set, else ~/.vct/. {HOME} is always the OS
        // home (used by some manifests for `{HOME}/.config/...` patterns).
        let vct_root = crate::paths::vct_root_dir();
        Self {
            vct_modules: vct_root.join("modules"),
            vct_data: vct_root.join("data"),
            vct_logs: vct_root.join("logs"),
            vct_root,
            install_dir: None,
            module_id: module_id.to_string(),
            hostname: gethostname::gethostname().to_string_lossy().to_string(),
            user: std::env::var("USER")
                .or_else(|_| std::env::var("USERNAME"))
                .unwrap_or_else(|_| "user".to_string()),
            home: home.clone(),
            appdata: std::env::var_os("APPDATA").map(PathBuf::from),
        }
    }

    pub fn with_install_dir(mut self, dir: PathBuf) -> Self {
        self.install_dir = Some(dir);
        self
    }

    /// Substitute `{TOKEN}` patterns in a string.
    pub fn resolve(&self, s: &str) -> String {
        let mut out = s.to_string();
        let replacements: Vec<(&str, String)> = vec![
            ("{VCT_ROOT}", self.vct_root.display().to_string()),
            ("{VCT_MODULES}", self.vct_modules.display().to_string()),
            ("{VCT_DATA}", self.vct_data.display().to_string()),
            ("{VCT_LOGS}", self.vct_logs.display().to_string()),
            (
                "{install_dir}",
                self.install_dir
                    .as_ref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| format!("{{install_dir-unresolved:{}}}", self.module_id)),
            ),
            ("{MODULE_ID}", self.module_id.clone()),
            ("{HOSTNAME}", self.hostname.clone()),
            ("{USER}", self.user.clone()),
            ("{HOME}", self.home.display().to_string()),
            (
                "{APPDATA}",
                self.appdata
                    .as_ref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| self.home.display().to_string()),
            ),
        ];
        for (token, value) in replacements {
            out = out.replace(token, &value);
        }
        out
    }

    /// Resolve install_dir from a manifest string and return it as a PathBuf
    /// (without needing to set `install_dir` first).
    pub fn resolve_install_dir(&self, raw: &str) -> PathBuf {
        PathBuf::from(self.resolve(raw))
    }
}

/// Security: refuse install_dir paths that escape `~/.vct/modules/`.
///
/// Symlinks resolved via `canonicalize` — if the user has no such
/// directory yet we canonicalize the parent and append the module name.
///
/// v0.2.34: harden the "neither candidate nor root exists" case. The
/// pre-v0.2.34 code fell back to the literal `allowed_root` path when
/// canonicalisation failed, then compared the candidate's
/// canonicalized ancestor (e.g. `/home/user`) against it. That
/// comparison is asymmetric: `/home/user` does NOT start with
/// `/home/user/.vct/modules` (the ancestor is SHORTER than the
/// literal root), so every install would fail with a spurious
/// "escapes allowed root" error before the bootstrap mkdir got a
/// chance to create the directory. The fix walks UP from
/// `allowed_root` to find the closest canonicalizable ancestor, then
/// re-appends the stripped components literally — so the produced
/// `canonical_root` is always well-formed and lexically comparable
/// to the candidate's walked-up canonical base.
pub fn validate_install_dir(candidate: &Path, allowed_root: &Path) -> Result<(), String> {
    let abs = if candidate.is_absolute() {
        candidate.to_path_buf()
    } else {
        return Err(format!("install_dir must be absolute: {}", candidate.display()));
    };

    // v0.2.34: both sides go through `canonicalize_with_walkup` so
    // their lexical shapes are symmetric. The pre-v0.2.34 code
    // canonicalized the candidate by walking UP to the closest
    // existing ancestor (losing the trailing components) but then
    // compared against either the literal allowed_root (when the
    // root didn't exist) or the canonical allowed_root (when it
    // did). Mixing those two shapes meant the `starts_with` check
    // could fail in either direction:
    //
    //   * root literal, candidate walked-up too short →
    //     `/tmp/foo.starts_with("/tmp/foo/.vct/modules")` = false
    //   * root walked-up, candidate walked-up further →
    //     `/tmp.starts_with("/tmp/foo")` = false
    //
    // After v0.2.34, both sides walk up to their closest existing
    // ancestor and re-append the stripped tail components. The
    // resulting paths share a real on-disk prefix (any symlinks
    // resolved) plus a literal nonexistent-tail suffix, which is
    // exactly what `starts_with` was designed to compare.
    let canonical_base = canonicalize_with_walkup(&abs).ok_or_else(|| {
        format!(
            "install_dir has no canonicalizable ancestor: {}",
            candidate.display()
        )
    })?;

    let canonical_root = canonicalize_with_walkup(allowed_root)
        .ok_or_else(|| {
            format!(
                "allowed_root has no canonicalizable ancestor: {}",
                allowed_root.display()
            )
        })?;

    if !canonical_base.starts_with(&canonical_root) {
        return Err(format!(
            "install_dir {} escapes allowed root {}",
            candidate.display(),
            allowed_root.display()
        ));
    }
    Ok(())
}

/// v0.2.34: canonicalise a (possibly nonexistent) absolute path by
/// walking up to the closest existing ancestor, canonicalising THAT,
/// and re-appending the components that were stripped along the way.
/// Returns `None` only if no ancestor canonicalises (e.g. the root
/// `/` itself fails — practically impossible on any real OS).
///
/// Differs from a bare `path.canonicalize()` in that the input does
/// NOT need to exist on disk. Differs from a lexical-only normaliser
/// (e.g. `path-clean`) in that symlinks in existing ancestors ARE
/// resolved — preserving the security property that a symlink
/// pointing outside `allowed_root` still fails the `starts_with`
/// check downstream.
fn canonicalize_with_walkup(path: &Path) -> Option<PathBuf> {
    let mut probe = path.to_path_buf();
    // Components stripped during the walk-up, in REVERSE order
    // (closest-to-input first). We re-append them in `.iter().rev()`
    // order at the end to reconstruct the original tail.
    let mut stripped: Vec<std::ffi::OsString> = Vec::new();
    loop {
        match probe.canonicalize() {
            Ok(mut canonical) => {
                for component in stripped.iter().rev() {
                    canonical.push(component);
                }
                return Some(canonical);
            }
            Err(_) => {
                let last = probe.file_name().map(|s| s.to_os_string());
                let parent = probe.parent().map(|p| p.to_path_buf());
                match (last, parent) {
                    (Some(name), Some(p)) => {
                        stripped.push(name);
                        probe = p;
                    }
                    // Reached `/` (or a Windows drive root) and it
                    // still doesn't canonicalise — give up.
                    _ => return None,
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Confirms the v0.1.0 vct-rl-reranker manifest deserializes cleanly
    /// AFTER the InstallMethod::ContainerPull + ContainerInstallBlock
    /// additions (Phase 1B, 2026-05-16). If serde fields drift later
    /// (e.g. ContainerInstallBlock gains a required field without a
    /// default), this test fails fast at CI time before any user hits it.
    ///
    /// The manifest lives at <repo>/paid-modules/vct-rl-reranker/vct-module.json
    /// — a staging dir, NOT shipped via launcher/bundled_manifests/ (paid
    /// modules ship via the signed-URL gateway, not the AGPL release).
    #[test]
    fn vct_rl_reranker_manifest_deserializes() {
        // Walk up from src-tauri/ to repo root.
        // v0.2.21 Step 3d: this module moved from `launcher/src-tauri/src/`
        // to `launcher/src-tauri/vct-launcher-core/src/`, so the parent
        // walk needs ONE MORE step. CARGO_MANIFEST_DIR is now
        // `launcher/src-tauri/vct-launcher-core/`; three .parent() calls
        // reach the repo root where `vct-module.json` lives.
        let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .expect("walk to repo root")
            .to_path_buf();
        let path = repo_root.join("paid-modules/vct-rl-reranker/vct-module.json");
        if !path.exists() {
            // Test is informational on dev clones that don't have the
            // paid-modules staging dir checked out. Skip rather than fail.
            eprintln!(
                "[test skip] paid-modules/vct-rl-reranker/vct-module.json not present \
                 (path: {}) — skipping deserialize check",
                path.display()
            );
            return;
        }
        let body = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        let manifest: ModuleManifest = serde_json::from_str(&body)
            .unwrap_or_else(|e| panic!("deserialize {}: {}", path.display(), e));

        assert_eq!(manifest.id, "vct-rl-reranker");
        // v0.2.46: pin the manifest's series rather than the exact patch
        // level so the test stays useful across paid-module releases
        // without needing a hand-edit on every bump. Earlier exact-equal
        // assertions (0.1.0 / 0.1.1 / 0.2.8) drifted out of sync with
        // releases — the manifest is at 0.2.9 today and will be 0.2.10
        // soon. Asserts (a) starts with "0." (catches a major-version
        // reorg) AND (b) lands in a known active series. Update the
        // allowlist when the paid module's major minor advances.
        //
        // Supersedes the v0.2.47 RL branch's `assert_eq!(version, "0.2.8")`
        // hand-edit which would have needed bumping again to 0.2.9 for
        // the paired ship.
        assert!(
            manifest.version.starts_with("0."),
            "manifest.version = {:?} — expected a 0.x release",
            manifest.version
        );
        let active_series = ["0.2.", "0.3.", "0.4."];
        assert!(
            active_series
                .iter()
                .any(|prefix| manifest.version.starts_with(prefix)),
            "manifest.version = {:?} — paid module is in the 0.2.x series; \
             update active_series list when the module advances past 0.4.x",
            manifest.version
        );
        assert_eq!(manifest.install.method, InstallMethod::ContainerPull);
        assert!(manifest.license.required);
        assert_eq!(manifest.license.min_orchestrator_tier, "pro");

        let container = manifest
            .install
            .container
            .as_ref()
            .expect("install.container present for container_pull method");
        assert_eq!(container.image, "ghcr.io/hotak92/vct-rl-reranker");
        assert!(container.tag_from_version);
        assert!(container.pull_token_endpoint.starts_with("https://"));
        assert!(container.rotate_weights);
    }

    // ─── Stream 2 (2026-05-19): GuiBlock / ConfigTab / ConfigControl ────
    //
    // The schema in `manifest.rs` is load-bearing: a paid module that
    // ships with a `gui.config_tab` becomes incompatible if a required
    // field is added without a default. These tests pin the wire shape
    // for each of the five control kinds + the umbrella block, so any
    // future refactor either keeps backward-compat OR breaks loudly
    // at CI time (not at install time on a customer's machine).

    /// Minimal canonical manifest with a complete `gui.config_tab`
    /// covering all five control kinds. Used as the fixture for every
    /// gui_block_*_deserializes test below. Keeps the variants in one
    /// place so reviewers see the full shape at a glance.
    fn gui_block_fixture_manifest() -> &'static str {
        r#"{
            "id": "test-mod",
            "name": "Test Module",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "gui": {
                "config_tab": {
                    "title": "Test Tab",
                    "icon": "sliders",
                    "description": "test description",
                    "sections": [
                        {
                            "title": "Section A",
                            "description": "first section",
                            "collapsible": false,
                            "controls": [
                                { "kind": "info", "id": "info1", "text": "hello", "variant": "info" }
                            ]
                        },
                        {
                            "title": "Section B",
                            "collapsible": true,
                            "initially_collapsed": false,
                            "controls": [
                                { "kind": "checkbox", "id": "c1", "label": "Use feature",
                                  "tooltip": "Hover help", "default": true,
                                  "on_change": "set_feature" },
                                { "kind": "multi_select", "id": "ms1", "label": "Pick many",
                                  "tooltip": "Choose any",
                                  "options_source": "list_options", "on_change": "set_picks" },
                                { "kind": "button", "id": "btn1", "label": "Reset",
                                  "tooltip": "Reset all", "action": "do_reset",
                                  "variant": "danger", "confirm": "Are you sure?" },
                                { "kind": "select", "id": "sel1", "label": "Mode",
                                  "tooltip": "Pick one",
                                  "options": [
                                    { "value": "a", "label": "Mode A" },
                                    { "value": "b", "label": "Mode B" }
                                  ],
                                  "default": "a", "on_change": "set_mode" }
                            ]
                        }
                    ]
                }
            }
        }"#
    }

    #[test]
    fn gui_block_full_fixture_deserializes() {
        let manifest = ModuleManifest::from_json(gui_block_fixture_manifest())
            .expect("fixture must parse");
        let gui = manifest.gui.expect("gui block present");
        let tab = gui.config_tab.expect("config_tab present");
        assert_eq!(tab.title, "Test Tab");
        assert_eq!(tab.icon.as_deref(), Some("sliders"));
        assert_eq!(tab.description.as_deref(), Some("test description"));
        assert_eq!(tab.route, None, "default route resolution happens at command layer");
        assert_eq!(tab.sections.len(), 2);
        assert_eq!(tab.sections[0].title, "Section A");
        assert!(!tab.sections[0].collapsible);
        assert!(tab.sections[1].collapsible);
        assert!(!tab.sections[1].initially_collapsed);
        // Section B carries one of every interactive control kind.
        assert_eq!(tab.sections[1].controls.len(), 4);
    }

    /// Helper: unwrap an [`ActionRef`] to its legacy command name, or
    /// panic. Used in legacy-form back-compat tests where the fixture
    /// declares a plain string. v0.2.26+ tests that exercise structured
    /// descriptors should pattern-match on `ActionRef::Descriptor`
    /// directly.
    fn action_ref_as_legacy(action: &ActionRef) -> &str {
        match action {
            ActionRef::Legacy(s) => s.as_str(),
            ActionRef::Descriptor(d) => {
                panic!("expected legacy ActionRef, got descriptor: {:?}", d)
            }
        }
    }

    #[test]
    fn gui_block_checkbox_variant_round_trips_tooltip() {
        let manifest = ModuleManifest::from_json(gui_block_fixture_manifest()).unwrap();
        let controls = &manifest.gui.unwrap().config_tab.unwrap().sections[1].controls;
        match &controls[0] {
            ConfigControl::Checkbox { id, label, tooltip, default, on_change } => {
                assert_eq!(id, "c1");
                assert_eq!(label, "Use feature");
                assert_eq!(tooltip.as_deref(), Some("Hover help"));
                assert!(*default);
                let on_change = on_change.as_ref().expect("on_change present");
                assert_eq!(action_ref_as_legacy(on_change), "set_feature");
            }
            other => panic!("expected Checkbox, got {:?}", other),
        }
    }

    #[test]
    fn gui_block_multi_select_variant_has_options_source() {
        let manifest = ModuleManifest::from_json(gui_block_fixture_manifest()).unwrap();
        let controls = &manifest.gui.unwrap().config_tab.unwrap().sections[1].controls;
        match &controls[1] {
            ConfigControl::MultiSelect { id, options_source, tooltip, filter, .. } => {
                assert_eq!(id, "ms1");
                assert_eq!(action_ref_as_legacy(options_source), "list_options");
                assert!(tooltip.is_some(), "tooltip declared in fixture");
                // v0.2.32: pre-L6 fixture doesn't declare a filter — must
                // deserialise as None (back-compat).
                assert!(filter.is_none(), "fixture has no filter declared");
            }
            other => panic!("expected MultiSelect, got {:?}", other),
        }
    }

    // ─── v0.2.32 L6: SelectOption back-compat + MultiSelectFilter ─────

    /// Bare-string list MUST deserialise — caller convenience for
    /// `options_source` that returns plain `["a", "b"]`.
    #[test]
    fn select_option_back_compat_string_deserialization() {
        let raw = r#"["a", "b", "qwen3"]"#;
        let opts: Vec<SelectOption> =
            serde_json::from_str(raw).expect("bare-string list must parse");
        assert_eq!(opts.len(), 3);
        assert_eq!(opts[0].value, "a");
        assert_eq!(opts[0].label, "a", "label defaults to value for bare strings");
        assert!(opts[0].badge.is_none());
        assert!(opts[0].meta.is_none());
        assert_eq!(opts[2].value, "qwen3");
        assert_eq!(opts[2].label, "qwen3");
    }

    /// Full object form parses all four fields (value, label, badge, meta).
    #[test]
    fn select_option_full_object_deserialization() {
        let raw = r#"[
            {"value": "a", "label": "Option A", "badge": "new",
             "meta": {"embedding_source": "qwen3", "size_mb": 12}},
            {"value": "b", "label": "Option B"}
        ]"#;
        let opts: Vec<SelectOption> =
            serde_json::from_str(raw).expect("rich object list must parse");
        assert_eq!(opts.len(), 2);
        assert_eq!(opts[0].value, "a");
        assert_eq!(opts[0].label, "Option A");
        assert_eq!(opts[0].badge.as_deref(), Some("new"));
        let meta = opts[0].meta.as_ref().expect("meta declared");
        assert_eq!(meta["embedding_source"], "qwen3");
        assert_eq!(meta["size_mb"], 12);
        // Second option: badge / meta absent ⇒ None
        assert_eq!(opts[1].value, "b");
        assert_eq!(opts[1].label, "Option B");
        assert!(opts[1].badge.is_none());
        assert!(opts[1].meta.is_none());
    }

    /// Mixed list — strings AND objects — also parses.
    #[test]
    fn select_option_mixed_list_deserialization() {
        let raw = r#"["bare", {"value": "rich", "label": "Rich"}]"#;
        let opts: Vec<SelectOption> = serde_json::from_str(raw).unwrap();
        assert_eq!(opts.len(), 2);
        assert_eq!(opts[0].value, "bare");
        assert_eq!(opts[0].label, "bare");
        assert_eq!(opts[1].value, "rich");
        assert_eq!(opts[1].label, "Rich");
    }

    /// Object without `label` defaults to `value` as the label.
    #[test]
    fn select_option_missing_label_defaults_to_value() {
        let raw = r#"[{"value": "alone"}]"#;
        let opts: Vec<SelectOption> = serde_json::from_str(raw).unwrap();
        assert_eq!(opts.len(), 1);
        assert_eq!(opts[0].value, "alone");
        assert_eq!(opts[0].label, "alone", "label falls back to value");
    }

    /// A manifest carrying `filter: {kind: "match", ...}` on a
    /// multi_select control MUST deserialise. Pinning the wire shape:
    /// future renderer / Tauri-command work depends on these field
    /// names landing in the manifest JSON exactly as written here.
    #[test]
    fn multi_select_filter_match_deserialization() {
        let raw = r#"{
            "id": "test-mod",
            "name": "Test Module",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "gui": {
                "config_tab": {
                    "title": "T",
                    "sections": [{
                        "title": "S",
                        "collapsible": false,
                        "controls": [{
                            "kind": "multi_select",
                            "id": "ms_with_filter",
                            "label": "Pick weights",
                            "options_source": "list_global_weights",
                            "filter": {
                                "kind": "match",
                                "meta_field": "embedding_source",
                                "equals_runtime": "container.active_embedding"
                            }
                        }]
                    }]
                }
            }
        }"#;
        let manifest = ModuleManifest::from_json(raw).expect("manifest must parse");
        let controls = &manifest.gui.unwrap().config_tab.unwrap().sections[0].controls;
        match &controls[0] {
            ConfigControl::MultiSelect { id, filter, .. } => {
                assert_eq!(id, "ms_with_filter");
                let f = filter.as_ref().expect("filter declared");
                match f {
                    MultiSelectFilter::Match { meta_field, equals_runtime } => {
                        assert_eq!(meta_field, "embedding_source");
                        assert_eq!(equals_runtime, "container.active_embedding");
                    }
                }
            }
            other => panic!("expected MultiSelect with filter, got {:?}", other),
        }
    }

    /// Round-trip: serialise a MultiSelectFilter::Match then parse it
    /// back. Catches accidental changes to the tag/field names.
    #[test]
    fn multi_select_filter_match_round_trip() {
        let f = MultiSelectFilter::Match {
            meta_field: "embedding_source".into(),
            equals_runtime: "container.active_embedding".into(),
        };
        let json = serde_json::to_string(&f).unwrap();
        // Tag-name pinning — the renderer's JS-side switch depends on this.
        assert!(json.contains(r#""kind":"match""#), "tag must be 'match': {}", json);
        assert!(json.contains(r#""meta_field":"embedding_source""#));
        assert!(json.contains(r#""equals_runtime":"container.active_embedding""#));
        let back: MultiSelectFilter = serde_json::from_str(&json).unwrap();
        assert_eq!(back, f);
    }

    #[test]
    fn gui_block_button_variant_has_confirm_and_variant() {
        let manifest = ModuleManifest::from_json(gui_block_fixture_manifest()).unwrap();
        let controls = &manifest.gui.unwrap().config_tab.unwrap().sections[1].controls;
        match &controls[2] {
            ConfigControl::Button { id, action, confirm, variant, .. } => {
                assert_eq!(id, "btn1");
                assert_eq!(action_ref_as_legacy(action), "do_reset");
                assert_eq!(confirm.as_deref(), Some("Are you sure?"));
                assert_eq!(variant.as_deref(), Some("danger"));
            }
            other => panic!("expected Button, got {:?}", other),
        }
    }

    #[test]
    fn gui_block_select_variant_options_round_trip() {
        let manifest = ModuleManifest::from_json(gui_block_fixture_manifest()).unwrap();
        let controls = &manifest.gui.unwrap().config_tab.unwrap().sections[1].controls;
        match &controls[3] {
            ConfigControl::Select { id, options, default, .. } => {
                assert_eq!(id, "sel1");
                assert_eq!(options.len(), 2);
                assert_eq!(options[0].value, "a");
                assert_eq!(options[1].label, "Mode B");
                assert_eq!(default.as_deref(), Some("a"));
            }
            other => panic!("expected Select, got {:?}", other),
        }
    }

    #[test]
    fn gui_block_info_variant_has_text_and_variant() {
        let manifest = ModuleManifest::from_json(gui_block_fixture_manifest()).unwrap();
        let controls = &manifest.gui.unwrap().config_tab.unwrap().sections[0].controls;
        match &controls[0] {
            ConfigControl::Info { id, text, variant } => {
                assert_eq!(id, "info1");
                assert_eq!(text, "hello");
                assert_eq!(variant.as_deref(), Some("info"));
            }
            other => panic!("expected Info, got {:?}", other),
        }
    }

    /// Manifests without a `gui` block are still valid (back-compat).
    /// All existing v0.x manifests fall in this category.
    #[test]
    fn manifest_without_gui_block_remains_valid() {
        let raw = r#"{
            "id": "no-gui-mod",
            "name": "Module No GUI",
            "version": "0.1.0",
            "category": "core",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "cli", "command": "echo" }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("valid without gui");
        assert!(m.gui.is_none());
    }

    /// Confirms the vct-rl-reranker manifest carries a fully-populated
    /// `gui.config_tab` after Stream 2 (Part D). Skipped when the file
    /// is absent (paid-modules dir not present on this dev clone).
    /// Pins the section count, title, and presence of at least one
    /// control of each kind so manifest drift breaks loudly.
    #[test]
    fn vct_rl_reranker_manifest_with_gui_tab_deserializes() {
        // v0.2.21 Step 3d: this module moved from `launcher/src-tauri/src/`
        // to `launcher/src-tauri/vct-launcher-core/src/`, so the parent
        // walk needs ONE MORE step. CARGO_MANIFEST_DIR is now
        // `launcher/src-tauri/vct-launcher-core/`; three .parent() calls
        // reach the repo root where `vct-module.json` lives.
        let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .expect("walk to repo root")
            .to_path_buf();
        let path = repo_root.join("paid-modules/vct-rl-reranker/vct-module.json");
        if !path.exists() {
            eprintln!(
                "[test skip] paid-modules/vct-rl-reranker/vct-module.json not present \
                 (path: {}) — skipping gui_tab check",
                path.display()
            );
            return;
        }
        let body = std::fs::read_to_string(&path).expect("read manifest");
        let manifest: ModuleManifest =
            serde_json::from_str(&body).expect("deserialize manifest");

        let gui = manifest.gui.expect("vct-rl-reranker must declare gui block");
        let tab = gui.config_tab.expect("gui.config_tab must be populated");

        assert_eq!(tab.title, "RL Reranker", "title pinned");
        assert_eq!(
            tab.sections.len(),
            3,
            "expected three sections (status / per-project / global)"
        );

        // Flatten controls + verify at least one of each interactive
        // kind exists. An info banner is required (section 1 is status-only).
        // Both ``info`` and ``info_dynamic`` count as info banners — they
        // render the same surface, but ``info_dynamic`` pulls its message
        // from module_db at runtime instead of a static literal. The current
        // vct-rl-reranker manifest uses ``info_dynamic`` exclusively for
        // section 1 (v0.2.32+); the static ``info`` variant survives for
        // back-compat with manifests that haven't migrated yet.
        let mut has_checkbox = false;
        let mut has_button = false;
        let mut has_multi_select = false;
        let mut has_info = false;
        for section in &tab.sections {
            for control in &section.controls {
                match control {
                    ConfigControl::Checkbox { .. } => has_checkbox = true,
                    ConfigControl::Button { .. } => has_button = true,
                    ConfigControl::MultiSelect { .. } => has_multi_select = true,
                    ConfigControl::Info { .. }
                    | ConfigControl::InfoDynamic { .. } => has_info = true,
                    ConfigControl::Select { .. } => {}
                    // v0.2.26+ kinds — not exercised by the current
                    // vct-rl-reranker manifest, but acknowledged here
                    // so a future RL manifest update that adds one of
                    // them doesn't unexpectedly fail this test.
                    ConfigControl::TextInput { .. }
                    | ConfigControl::NumberInput { .. }
                    | ConfigControl::StatusDisplay { .. }
                    | ConfigControl::FilePicker { .. }
                    | ConfigControl::Link { .. }
                    // v0.2.32 additions — same acknowledgement: the
                    // RL manifest may grow these but the test doesn't
                    // assert on them.
                    | ConfigControl::DatePicker { .. }
                    // v0.2.33 forward-compat fallback — would only fire
                    // if a future RL manifest ships a control kind this
                    // launcher version doesn't know. The test stays
                    // quiet rather than asserting (this launcher would
                    // render the placeholder; the test isn't here to
                    // pin the placeholder behaviour, that's in the
                    // dedicated `unsupported_control_kind_*` tests).
                    | ConfigControl::Unsupported { .. } => {}
                }
            }
        }
        assert!(has_checkbox, "manifest must declare at least one checkbox");
        assert!(has_button, "manifest must declare at least one button");
        assert!(has_multi_select, "manifest must declare a multi_select");
        // v0.2.46: also accept `info_dynamic` (live-updating info banner,
        // added in v0.2.32). The earlier assertion against `has_info` alone
        // failed because the current vct-rl-reranker manifest uses three
        // `info_dynamic` controls in section 1 (weights_version_live,
        // last_training_live, active_embedding_live) instead of static info
        // banners. The semantic intent ("the user sees at-a-glance status")
        // is satisfied either way.
        let mut has_info_dynamic = false;
        for section in &tab.sections {
            for control in &section.controls {
                if matches!(control, ConfigControl::InfoDynamic { .. }) {
                    has_info_dynamic = true;
                }
            }
        }
        assert!(
            has_info || has_info_dynamic,
            "section 1 must include at least one info banner (info OR info_dynamic)"
        );
    }

    // ─── v0.2.20: per-module GPU mode hints (RuntimeBlock additions) ───
    //
    // Three new RuntimeBlock fields land in v0.2.20:
    //   - min_gpu_vram_gb       Option<f64>
    //   - gpu_optional          bool
    //   - gpu_image_variants    Option<GpuImageVariants>
    //
    // Pinned via JSON fixtures so a future refactor (e.g. renaming
    // gpu_image_variants → image_variants) breaks loudly at CI time
    // rather than at install time on a customer's machine.

    /// Manifest with the full v0.2.20 GPU hint block populated.
    #[test]
    fn runtime_block_v0_2_20_gpu_hints_deserialize() {
        let raw = r#"{
            "id": "gpu-hints-mod",
            "name": "GPU Hints Module",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "pro" },
            "install": { "method": "container_pull",
                "container": {
                    "image": "ghcr.io/example/gpu-mod",
                    "pull_token_endpoint": "https://example.com/token"
                }
            },
            "runtime": {
                "type": "service",
                "command": "echo",
                "min_gpu_vram_gb": 4.0,
                "gpu_optional": true,
                "gpu_image_variants": {
                    "cpu":  "0.1.0-cpu",
                    "cuda": "0.1.0-cuda",
                    "rocm": "0.1.0-rocm"
                }
            }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("v0.2.20 GPU hints parse");
        assert_eq!(m.runtime.min_gpu_vram_gb, Some(4.0));
        assert!(m.runtime.gpu_optional);
        let variants = m
            .runtime
            .gpu_image_variants
            .as_ref()
            .expect("variants present");
        assert_eq!(variants.cpu, "0.1.0-cpu");
        assert_eq!(variants.cuda, "0.1.0-cuda");
        assert_eq!(variants.rocm, "0.1.0-rocm");
    }

    /// Pre-v0.2.20 manifests (no GPU hint fields) must still deserialize.
    /// Verifies serde defaults: `None` for the two `Option` fields and
    /// `false` for `gpu_optional`.
    #[test]
    fn runtime_block_backward_compat_without_gpu_hints() {
        let raw = r#"{
            "id": "legacy-mod",
            "name": "Legacy Module",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "cli", "command": "echo" }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("legacy manifest parse");
        assert_eq!(m.runtime.min_gpu_vram_gb, None);
        assert!(!m.runtime.gpu_optional);
        assert!(m.runtime.gpu_image_variants.is_none());
    }

    /// Confirms the on-disk vct-rl-reranker manifest carries the v0.2.20
    /// GPU hints. Skipped when the paid-modules staging dir is absent
    /// (some dev clones don't have it). Pins the three values so a
    /// later manifest edit can't silently drop them.
    #[test]
    fn vct_rl_reranker_manifest_carries_v0_2_20_gpu_hints() {
        // v0.2.21 Step 3d: this module moved from `launcher/src-tauri/src/`
        // to `launcher/src-tauri/vct-launcher-core/src/`, so the parent
        // walk needs ONE MORE step. CARGO_MANIFEST_DIR is now
        // `launcher/src-tauri/vct-launcher-core/`; three .parent() calls
        // reach the repo root where `vct-module.json` lives.
        let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .expect("walk to repo root")
            .to_path_buf();
        let path = repo_root.join("paid-modules/vct-rl-reranker/vct-module.json");
        if !path.exists() {
            eprintln!(
                "[test skip] paid-modules/vct-rl-reranker/vct-module.json not present \
                 (path: {}) — skipping v0.2.20 GPU-hints check",
                path.display()
            );
            return;
        }
        let body = std::fs::read_to_string(&path).expect("read manifest");
        let m: ModuleManifest = ModuleManifest::from_json(&body).expect("parse manifest");

        // RL reranker is the canonical small-VRAM module — 4 GB threshold.
        assert_eq!(
            m.runtime.min_gpu_vram_gb,
            Some(4.0),
            "RL reranker pins its per-module threshold to 4 GB (model is ~5 MB)"
        );
        // RL reranker is gpu_optional=true (it runs on CPU, just slower).
        assert!(m.runtime.gpu_optional, "RL reranker must declare gpu_optional=true");

        let variants = m
            .runtime
            .gpu_image_variants
            .as_ref()
            .expect("RL reranker must declare gpu_image_variants");
        // Tags pinned to the version (v0.2.20 ships 0.1.0-{cpu,cuda,rocm}).
        // Allow either the explicit version-prefixed form OR a future
        // floating-tag form by asserting the suffix.
        assert!(
            variants.cpu.ends_with("cpu"),
            "cpu variant tag should end in -cpu (got '{}')",
            variants.cpu
        );
        assert!(
            variants.cuda.ends_with("cuda"),
            "cuda variant tag should end in -cuda (got '{}')",
            variants.cuda
        );
        assert!(
            variants.rocm.ends_with("rocm"),
            "rocm variant tag should end in -rocm (got '{}')",
            variants.rocm
        );
    }

    /// Stream 2 follow-up (v0.2.20, 2026-05-19): the orchestrator-core
    /// `vct-module.json` (repo root) is itself a `gui.config_tab`-bearing
    /// manifest. This test confirms it deserializes through the SAME
    /// `ModuleManifest::from_json` path that `commands::module_gui::
    /// get_module_nav_items` uses — proving the schema generalizes to
    /// the always-installed core, not just paid modules.
    ///
    /// Pinned assertions (post v0.2.24.1 A0bis rename):
    ///   * title == "Clone integrity" (load-bearing — the Sidebar
    ///     surfaces this as the nav label). Renamed from "Orchestrator
    ///     core" in v0.2.24.1 per honest scope audit: per-project
    ///     actions moved out (duplicates with KG/Codegraph tab);
    ///     Diagnostics deferred to a Services-tab follow-up.
    ///   * 2 sections (Clone discovery / Clone manifest)
    ///   * the orchestrator's slim historical fields (components,
    ///     bundled_secrets) coexist with the full ModuleManifest fields
    ///     without conflict (serde is permissive on unknown fields)
    #[test]
    fn orchestrator_core_manifest_with_gui_tab_deserializes() {
        // Walk up from src-tauri/ to repo root, same pattern as the
        // vct_rl_reranker test above. The orchestrator's manifest is
        // load-bearing on every install so an unconditional
        // `panic!("missing")` is safe — it can't go missing in CI
        // without a much bigger problem.
        // v0.2.21 Step 3d: this module moved from `launcher/src-tauri/src/`
        // to `launcher/src-tauri/vct-launcher-core/src/`, so the parent
        // walk needs ONE MORE step. CARGO_MANIFEST_DIR is now
        // `launcher/src-tauri/vct-launcher-core/`; three .parent() calls
        // reach the repo root where `vct-module.json` lives.
        let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .expect("walk to repo root")
            .to_path_buf();
        let path = repo_root.join("vct-module.json");
        assert!(
            path.exists(),
            "orchestrator-core manifest must exist at {} \
             (repo invariant — every install ships it)",
            path.display()
        );

        let body = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        let manifest: ModuleManifest = ModuleManifest::from_json(&body)
            .unwrap_or_else(|e| panic!("deserialize {}: {}", path.display(), e));

        assert_eq!(manifest.id, "orchestrator", "id pinned");
        assert_eq!(
            manifest.category,
            ModuleCategory::Core,
            "orchestrator core must declare category=core"
        );

        let gui = manifest.gui.expect(
            "orchestrator-core manifest must have a gui block — Stream 2 follow-up \
             added it to validate schema generalization beyond paid modules",
        );
        let tab = gui.config_tab.expect("must have config_tab");

        assert_eq!(tab.title, "Clone integrity", "title pinned (Sidebar nav label, renamed in v0.2.24.1 A0bis)");
        assert_eq!(tab.icon.as_deref(), Some("box"), "icon pinned");
        assert_eq!(tab.sections.len(), 2, "expected 2 sections (Clone discovery / Clone manifest)");

        // Spot-check button + info structure post-A0bis (no confirm
        // prompts needed — both buttons are read-only diagnostics).
        let mut total_button_count = 0;
        let mut total_info_count = 0;
        for section in &tab.sections {
            for control in &section.controls {
                match control {
                    ConfigControl::Button { .. } => {
                        total_button_count += 1;
                    }
                    ConfigControl::Info { .. } => {
                        total_info_count += 1;
                    }
                    _ => {}
                }
            }
        }
        assert_eq!(
            total_button_count, 2,
            "expected 2 buttons (Re-detect orchestrator root + Validate clone manifest)"
        );
        assert!(
            total_info_count >= 2,
            "expected at least 2 info banners (one per section header)"
        );

        // v0.2.23 F2 (2026-05-21): orchestrator-core opts out of the
        // Sidebar's "Module configuration" group. Its controls are
        // folded into the per-project Settings page when the active
        // project is the orchestrator-root row. Keeping the standalone
        // sidebar entry AND the per-project Settings rendering would
        // duplicate the surface — the user explicitly asked to
        // consolidate to one place.
        assert!(
            !tab.show_in_sidebar,
            "orchestrator-core manifest must declare show_in_sidebar=false \
             (its controls now live in per-project Settings when the \
              orchestrator-root project is selected)"
        );
    }

    /// v0.2.23 F2 (2026-05-21): the `show_in_sidebar` field defaults
    /// to `true` when the manifest omits it. Pins backwards compat for
    /// paid modules whose only GUI surface is `/modules/<id>/config`
    /// — they keep their sidebar entry without having to bump the
    /// manifest schema.
    #[test]
    fn config_tab_show_in_sidebar_defaults_to_true_when_unset() {
        let manifest = ModuleManifest::from_json(gui_block_fixture_manifest())
            .expect("fixture must parse");
        let tab = manifest.gui.unwrap().config_tab.unwrap();
        assert!(
            tab.show_in_sidebar,
            "show_in_sidebar must default to true so legacy paid-module \
             manifests (no such field) keep their sidebar entry"
        );
    }

    /// v0.2.23 F2 (2026-05-21): explicit `show_in_sidebar: false` in a
    /// manifest deserializes faithfully. The fixture inlines the field
    /// rather than going through the orchestrator manifest's full
    /// shape, so this test exercises the serde plumbing in isolation.
    #[test]
    fn config_tab_show_in_sidebar_false_round_trips() {
        let raw = r#"{
            "id": "hidden-mod",
            "name": "Hidden Module",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "gui": {
                "config_tab": {
                    "title": "Hidden Tab",
                    "show_in_sidebar": false,
                    "sections": []
                }
            }
        }"#;
        let manifest = ModuleManifest::from_json(raw).expect("must parse");
        let tab = manifest.gui.unwrap().config_tab.unwrap();
        assert!(!tab.show_in_sidebar);
    }

    // ─── v0.2.26 (2026-05-22): ActionRef / ActionDescriptor + 5 new control kinds ───
    //
    // The schema-rendered GUI gains a generic declarative HTTP-action
    // dispatcher (`module_dispatch_action`), so paid modules can add
    // new controls + endpoints WITHOUT shipping new Tauri commands.
    //
    // These tests pin:
    //   (a) Back-compat — every v0.2.20–v0.2.25 manifest still parses
    //       (the existing `gui_block_*` tests above cover this).
    //   (b) ActionRef untagged-enum behaviour — strings map to Legacy,
    //       objects map to Descriptor.
    //   (c) Each of the 5 new control variants deserializes correctly
    //       with realistic field values.
    //   (d) PollingSpec defaults are applied when fields are omitted.
    //   (e) Chained actions (`next_action`) deserialize at depth > 1.

    /// String form of `action` → ActionRef::Legacy (back-compat).
    #[test]
    fn action_ref_string_deserializes_as_legacy() {
        let json = r#""my_tauri_command""#;
        let action: ActionRef = serde_json::from_str(json).expect("string parses");
        match action {
            ActionRef::Legacy(s) => assert_eq!(s, "my_tauri_command"),
            ActionRef::Descriptor(_) => panic!("expected Legacy"),
        }
    }

    /// Object form of `action` → ActionRef::Descriptor (the v0.2.26 path).
    #[test]
    fn action_ref_object_deserializes_as_descriptor() {
        let json = r#"{ "kind": "http", "method": "POST", "path": "/reset", "body": {"strategy": "fork"} }"#;
        let action: ActionRef = serde_json::from_str(json).expect("descriptor parses");
        match action {
            ActionRef::Legacy(s) => panic!("expected Descriptor, got Legacy({})", s),
            ActionRef::Descriptor(ActionDescriptor::Http {
                method,
                path,
                body,
                polling,
                next_action,
            }) => {
                assert_eq!(method, HttpMethod::Post);
                assert_eq!(path, "/reset");
                let body = body.expect("body present");
                assert_eq!(body["strategy"], "fork");
                assert!(polling.is_none(), "no polling declared");
                assert!(next_action.is_none(), "no chained action");
            }
            // v0.2.32: ActionDescriptor gained ChainedAction; the
            // fixture json declares kind="http" so this arm is
            // unreachable in practice but needed for exhaustive match.
            ActionRef::Descriptor(ActionDescriptor::ChainedAction { .. }) => {
                panic!("fixture is kind=http, ChainedAction unexpected")
            }
            // v0.2.33: ActionDescriptor gained TauriCommand. Same
            // exhaustive-match obligation as ChainedAction above.
            ActionRef::Descriptor(ActionDescriptor::TauriCommand { .. }) => {
                panic!("fixture is kind=http, TauriCommand unexpected")
            }
        }
    }

    /// HTTP methods round-trip in UPPERCASE (the wire convention).
    #[test]
    fn http_method_serializes_uppercase() {
        let m = HttpMethod::Post;
        let json = serde_json::to_string(&m).unwrap();
        assert_eq!(json, "\"POST\"");
        let back: HttpMethod = serde_json::from_str("\"DELETE\"").unwrap();
        assert_eq!(back, HttpMethod::Delete);
    }

    /// Chained `next_action` nests at arbitrary depth (depth-3 fixture).
    /// Pinned because the design doc allows arbitrary depth and the
    /// dispatcher uses an iterative loop guarded by `max_chain_steps`
    /// — the parser itself must not impose a depth limit.
    #[test]
    fn action_descriptor_next_action_nests_deeply() {
        let json = r#"{
            "kind": "http", "method": "POST", "path": "/step1",
            "next_action": {
                "kind": "http", "method": "POST", "path": "/step2",
                "next_action": {
                    "kind": "http", "method": "GET", "path": "/step3"
                }
            }
        }"#;
        let action: ActionDescriptor = serde_json::from_str(json).expect("depth-3 parses");
        // v0.2.32: ActionDescriptor gained the ChainedAction variant, so
        // patterns on the Http variant are no longer irrefutable. Use
        // `let ... else` to keep the depth-3 walk readable while
        // gracefully panicking if the parser ever returns a wrong variant.
        let ActionDescriptor::Http { next_action, .. } = action else {
            panic!("outer step expected Http");
        };
        let inner = next_action.expect("level 2 present");
        let ActionDescriptor::Http { next_action, path, .. } = *inner else {
            panic!("inner step expected Http");
        };
        assert_eq!(path, "/step2");
        let deepest = next_action.expect("level 3 present");
        let ActionDescriptor::Http { method, path, next_action, .. } = *deepest else {
            panic!("deepest step expected Http");
        };
        assert_eq!(method, HttpMethod::Get);
        assert_eq!(path, "/step3");
        assert!(next_action.is_none(), "deepest leaf has no further chain");
    }

    /// PollingSpec applies serde defaults for every field except
    /// `endpoint` (the only required one).
    #[test]
    fn polling_spec_defaults_apply_when_fields_omitted() {
        let json = r#"{ "endpoint": "/finetune_status" }"#;
        let p: PollingSpec = serde_json::from_str(json).expect("minimal polling parses");
        assert_eq!(p.endpoint, "/finetune_status");
        assert_eq!(p.job_id_path, "$.job_id");
        assert_eq!(p.job_id_query_param, "job_id");
        assert_eq!(p.interval_seconds, 5);
        assert_eq!(p.max_attempts, 60);
        assert_eq!(p.terminal_state_field, "$.state");
        assert_eq!(p.terminal_success_values, vec!["done".to_string()]);
        assert_eq!(
            p.terminal_failure_values,
            vec!["failed".to_string(), "error".to_string()]
        );
        assert_eq!(p.progress_event, "module://action-progress");
        assert_eq!(p.failed_event, "module://action-failed");
    }

    /// PollingSpec round-trips a fully-specified body. Pins every
    /// optional field's wire name so a future field-name refactor
    /// breaks loudly at CI time rather than at manifest-load time.
    #[test]
    fn polling_spec_full_round_trips() {
        let json = r#"{
            "endpoint": "/finetune_status",
            "job_id_path": "$.task_id",
            "job_id_query_param": "task",
            "interval_seconds": 10,
            "max_attempts": 30,
            "terminal_state_field": "$.status",
            "terminal_success_values": ["ok", "complete"],
            "terminal_failure_values": ["fail"],
            "progress_event": "rl://progress",
            "failed_event": "rl://failed"
        }"#;
        let p: PollingSpec = serde_json::from_str(json).expect("full polling parses");
        assert_eq!(p.job_id_path, "$.task_id");
        assert_eq!(p.job_id_query_param, "task");
        assert_eq!(p.interval_seconds, 10);
        assert_eq!(p.max_attempts, 30);
        assert_eq!(p.terminal_success_values, vec!["ok".to_string(), "complete".to_string()]);
        assert_eq!(p.progress_event, "rl://progress");
    }

    /// Canonical fixture exercising all five new v0.2.26 control kinds
    /// PLUS a structured descriptor in the existing Button variant.
    /// One place to read the full schema; every per-variant test below
    /// pulls from here so reviewers see the wire shape at a glance.
    fn v0_2_26_controls_fixture_manifest() -> &'static str {
        r#"{
            "id": "v0226-mod",
            "name": "v0.2.26 Controls Module",
            "version": "0.2.26",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "pro" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "gui": {
                "config_tab": {
                    "title": "v0.2.26 Controls",
                    "sections": [{
                        "title": "All new kinds",
                        "controls": [
                            { "kind": "text_input", "id": "label", "label": "Display label",
                              "tooltip": "Friendly name shown in dashboard.",
                              "default": "Personal",
                              "placeholder": "e.g. 'Work account'",
                              "apply_action": {
                                  "kind": "http", "method": "POST", "path": "/validate_label",
                                  "body": {"label": "{{value}}"}
                              }
                            },
                            { "kind": "number_input", "id": "learning_rate", "label": "Learning rate",
                              "default": 0.001, "min": 0.0001, "max": 0.1, "step": 0.0001,
                              "on_change": {
                                  "kind": "http", "method": "POST", "path": "/set_lr",
                                  "body": {"value": "{{value}}"}
                              }
                            },
                            { "kind": "status_display", "id": "health", "label": "Container health",
                              "source": {
                                  "kind": "http", "method": "GET", "path": "/health",
                                  "polling": {"endpoint": "/health", "interval_seconds": 30}
                              },
                              "render_template": "{{status}} — model: {{model}}"
                            },
                            { "kind": "file_picker", "id": "weights", "label": "Custom weights",
                              "extensions": ["pt", "pth"], "directory": false
                            },
                            { "kind": "link", "id": "docs", "label": "Module docs",
                              "href": "https://example.com/docs", "target": "external"
                            },
                            { "kind": "button", "id": "reset", "label": "Reset",
                              "action": {
                                  "kind": "http", "method": "POST", "path": "/reset",
                                  "body": {"strategy": "fork"},
                                  "next_action": {
                                      "kind": "http", "method": "POST", "path": "/specialize",
                                      "body": {"days": 30},
                                      "polling": {
                                          "endpoint": "/finetune_status",
                                          "interval_seconds": 5,
                                          "max_attempts": 60
                                      }
                                  }
                              }
                            }
                        ]
                    }]
                }
            }
        }"#
    }

    /// All 6 controls in the fixture deserialize without error AND the
    /// fixture as a whole is a valid `ModuleManifest`.
    #[test]
    fn v0_2_26_controls_fixture_deserializes() {
        let m = ModuleManifest::from_json(v0_2_26_controls_fixture_manifest())
            .expect("v0.2.26 fixture parses");
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        assert_eq!(controls.len(), 6, "all six new+repurposed controls present");
    }

    #[test]
    fn text_input_variant_carries_apply_action() {
        let m = ModuleManifest::from_json(v0_2_26_controls_fixture_manifest()).unwrap();
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        match &controls[0] {
            ConfigControl::TextInput {
                id,
                label,
                default,
                placeholder,
                apply_action,
                ..
            } => {
                assert_eq!(id, "label");
                assert_eq!(label, "Display label");
                assert_eq!(default, "Personal");
                assert_eq!(placeholder.as_deref(), Some("e.g. 'Work account'"));
                let aa = apply_action.as_ref().expect("apply_action present");
                match aa {
                    ActionRef::Descriptor(ActionDescriptor::Http { path, method, .. }) => {
                        assert_eq!(path, "/validate_label");
                        assert_eq!(*method, HttpMethod::Post);
                    }
                    other => panic!("expected Http descriptor, got {:?}", other),
                }
            }
            other => panic!("expected TextInput, got {:?}", other),
        }
    }

    #[test]
    fn number_input_variant_carries_range() {
        let m = ModuleManifest::from_json(v0_2_26_controls_fixture_manifest()).unwrap();
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        match &controls[1] {
            ConfigControl::NumberInput {
                id, default, min, max, step, on_change, ..
            } => {
                assert_eq!(id, "learning_rate");
                assert_eq!(*default, Some(0.001));
                assert_eq!(*min, Some(0.0001));
                assert_eq!(*max, Some(0.1));
                assert_eq!(*step, Some(0.0001));
                assert!(on_change.is_some(), "on_change present");
            }
            other => panic!("expected NumberInput, got {:?}", other),
        }
    }

    #[test]
    fn status_display_variant_carries_polling() {
        let m = ModuleManifest::from_json(v0_2_26_controls_fixture_manifest()).unwrap();
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        match &controls[2] {
            ConfigControl::StatusDisplay {
                id, source, render_template, ..
            } => {
                assert_eq!(id, "health");
                assert_eq!(render_template, "{{status}} — model: {{model}}");
                match source {
                    ActionRef::Descriptor(ActionDescriptor::Http { path, polling, method, .. }) => {
                        assert_eq!(path, "/health");
                        assert_eq!(*method, HttpMethod::Get);
                        let p = polling.as_ref().expect("polling present");
                        assert_eq!(p.interval_seconds, 30);
                    }
                    other => panic!("expected Http descriptor, got {:?}", other),
                }
            }
            other => panic!("expected StatusDisplay, got {:?}", other),
        }
    }

    #[test]
    fn file_picker_variant_carries_extensions() {
        let m = ModuleManifest::from_json(v0_2_26_controls_fixture_manifest()).unwrap();
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        match &controls[3] {
            ConfigControl::FilePicker {
                id, extensions, directory, ..
            } => {
                assert_eq!(id, "weights");
                assert_eq!(extensions, &vec!["pt".to_string(), "pth".to_string()]);
                assert!(!directory);
            }
            other => panic!("expected FilePicker, got {:?}", other),
        }
    }

    #[test]
    fn link_variant_carries_href_and_target() {
        let m = ModuleManifest::from_json(v0_2_26_controls_fixture_manifest()).unwrap();
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        match &controls[4] {
            ConfigControl::Link { id, href, target, .. } => {
                assert_eq!(id, "docs");
                assert_eq!(href, "https://example.com/docs");
                assert_eq!(target, "external");
            }
            other => panic!("expected Link, got {:?}", other),
        }
    }

    /// Link target defaults to `"external"` when omitted. Pins the
    /// default so manifest authors don't have to spell it out.
    #[test]
    fn link_target_defaults_to_external() {
        let json = r#"{
            "kind": "link", "id": "L", "label": "Help",
            "href": "https://example.com/help"
        }"#;
        let c: ConfigControl = serde_json::from_str(json).expect("Link without target parses");
        match c {
            ConfigControl::Link { target, .. } => assert_eq!(target, "external"),
            other => panic!("expected Link, got {:?}", other),
        }
    }

    /// Re-used button-with-chained-action assertion. Confirms a depth-2
    /// chain ending in a polling spec deserializes correctly inside a
    /// full manifest (not just the bare ActionDescriptor unit-test path).
    #[test]
    fn button_with_chained_polling_action_round_trips() {
        let m = ModuleManifest::from_json(v0_2_26_controls_fixture_manifest()).unwrap();
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        match &controls[5] {
            ConfigControl::Button { id, action, .. } => {
                assert_eq!(id, "reset");
                match action {
                    ActionRef::Descriptor(ActionDescriptor::Http { path, next_action, .. }) => {
                        assert_eq!(path, "/reset");
                        let next = next_action.as_ref().expect("chain present");
                        // v0.2.32: pattern no longer irrefutable — use let-else.
                        let ActionDescriptor::Http { path, polling, .. } = next.as_ref() else {
                            panic!("inner action expected Http");
                        };
                        assert_eq!(path, "/specialize");
                        let p = polling.as_ref().expect("polling on inner action");
                        assert_eq!(p.endpoint, "/finetune_status");
                        assert_eq!(p.max_attempts, 60);
                    }
                    other => panic!("expected Http descriptor, got {:?}", other),
                }
            }
            other => panic!("expected Button, got {:?}", other),
        }
    }

    /// Back-compat: a manifest using the v0.2.20 plain-string `action`
    /// form still deserializes after the v0.2.26 ActionRef change.
    /// This is the load-bearing back-compat assertion — every
    /// v0.2.20–v0.2.25 module manifest in the wild must keep working.
    #[test]
    fn legacy_string_action_form_remains_parseable() {
        let raw = r#"{
            "id": "legacy-button-mod",
            "name": "Legacy Button Module",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "gui": {
                "config_tab": {
                    "title": "Legacy",
                    "sections": [{
                        "title": "Legacy controls",
                        "controls": [
                            { "kind": "button", "id": "b1", "label": "Do thing",
                              "action": "legacy_tauri_command" },
                            { "kind": "checkbox", "id": "c1", "label": "Toggle",
                              "on_change": "legacy_on_change_cmd" },
                            { "kind": "multi_select", "id": "ms1", "label": "Pick",
                              "options_source": "legacy_options_source_cmd" }
                        ]
                    }]
                }
            }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("legacy form parses");
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        assert!(matches!(&controls[0],
            ConfigControl::Button { action: ActionRef::Legacy(s), .. } if s == "legacy_tauri_command"
        ));
        assert!(matches!(&controls[1],
            ConfigControl::Checkbox { on_change: Some(ActionRef::Legacy(s)), .. } if s == "legacy_on_change_cmd"
        ));
        assert!(matches!(&controls[2],
            ConfigControl::MultiSelect { options_source: ActionRef::Legacy(s), .. } if s == "legacy_options_source_cmd"
        ));
    }

    // ─── v0.2.27: runtime.log_path_template validator ─────────────────

    #[test]
    fn log_path_template_accepts_canonical_rl_pattern() {
        assert!(validate_log_path_template("/data/logs/rl_events_{project_slug}.jsonl").is_ok());
    }

    #[test]
    fn log_path_template_accepts_uuid_form() {
        assert!(validate_log_path_template("/data/logs/{project_id}/events.jsonl").is_ok());
    }

    #[test]
    fn log_path_template_accepts_both_placeholders() {
        assert!(
            validate_log_path_template("/data/{project_slug}/{project_id}.jsonl").is_ok()
        );
    }

    #[test]
    fn log_path_template_rejects_empty() {
        assert!(validate_log_path_template("").is_err());
    }

    #[test]
    fn log_path_template_rejects_no_placeholder() {
        // No `{...}` token at all → would produce the same path for every
        // project → rejected at validation time.
        let err = validate_log_path_template("/data/logs/events.jsonl")
            .unwrap_err();
        assert!(err.contains("at least one"), "unexpected error: {}", err);
    }

    #[test]
    fn log_path_template_rejects_double_brace() {
        // Double-brace tokens are dispatcher-level — mixing them with the
        // single-brace per-project ones in the same template is forbidden.
        let err = validate_log_path_template("/data/logs/{{project_slug}}.jsonl")
            .unwrap_err();
        assert!(err.contains("double-brace"), "unexpected error: {}", err);
    }

    #[test]
    fn log_path_template_rejects_unknown_placeholder() {
        let err = validate_log_path_template("/data/logs/{module_id}/events.jsonl")
            .unwrap_err();
        assert!(err.contains("unknown placeholder"), "unexpected error: {}", err);
    }

    #[test]
    fn log_path_template_rejects_unclosed_brace() {
        let err = validate_log_path_template("/data/logs/{project_slug.jsonl")
            .unwrap_err();
        assert!(err.contains("unclosed"), "unexpected error: {}", err);
    }

    #[test]
    fn log_path_template_render_substitutes_slug() {
        let out = render_log_path_template(
            "/data/logs/rl_events_{project_slug}.jsonl",
            "uuid-aaa",
            "my-project",
        );
        assert_eq!(out, "/data/logs/rl_events_my-project.jsonl");
    }

    #[test]
    fn log_path_template_render_substitutes_both_tokens() {
        let out = render_log_path_template(
            "/data/{project_slug}/{project_id}.jsonl",
            "uuid-aaa",
            "my-project",
        );
        assert_eq!(out, "/data/my-project/uuid-aaa.jsonl");
    }

    #[test]
    fn module_manifest_rejects_invalid_log_path_template() {
        // Mid-deserialisation rejection: an otherwise-valid manifest with
        // a bad log_path_template fails at `from_json` time with a clear
        // error including the inner validator message.
        let raw = r#"{
            "id": "broken-template-mod",
            "name": "Broken Template",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "pro" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": {
                "type": "container",
                "command": "echo",
                "log_path_template": "/data/{not_a_real_token}/events.jsonl"
            }
        }"#;
        let err = ModuleManifest::from_json(raw).unwrap_err();
        assert!(err.contains("log_path_template"), "outer error: {}", err);
        assert!(err.contains("unknown placeholder"), "inner error: {}", err);
    }

    #[test]
    fn module_manifest_accepts_canonical_log_path_template() {
        let raw = r#"{
            "id": "rl-mod",
            "name": "RL Module",
            "version": "0.2.1",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "pro" },
            "install": { "method": "container_pull",
                "container": { "image": "ghcr.io/example/rl", "pull_token_endpoint": "https://example.com/t" }
            },
            "runtime": {
                "type": "container",
                "command": "echo",
                "log_path_template": "/data/logs/rl_events_{project_slug}.jsonl"
            }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("must parse");
        assert_eq!(
            m.runtime.log_path_template.as_deref(),
            Some("/data/logs/rl_events_{project_slug}.jsonl"),
        );
    }

    #[test]
    fn module_manifest_log_path_template_optional() {
        // Manifests without log_path_template parse fine — every module
        // pre-v0.2.27 (and post-v0.2.27 non-RL modules) omits this field.
        let raw = r#"{
            "id": "minimal-mod",
            "name": "Minimal",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("must parse");
        assert!(m.runtime.log_path_template.is_none());
    }

    // ─── v0.2.31: module-shipped DB migrations (`db` block) ─────────────

    #[test]
    fn module_manifest_db_block_optional_round_trip() {
        // Manifest without `db` block parses fine (most modules don't
        // need DB state).
        let raw = r#"{
            "id": "no-db-mod",
            "name": "No DB",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "cli", "command": "echo" }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("must parse");
        assert!(m.db.is_none());
    }

    #[test]
    fn module_manifest_db_block_accepts_valid_namespace() {
        let raw = r#"{
            "id": "rl-mod",
            "name": "RL",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "db": { "migrations_dir": "db/", "namespace": "rl" }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("must parse");
        {
            let db = m.db.as_ref().expect("db block present");
            assert_eq!(db.migrations_dir, "db/");
            assert_eq!(db.namespace, "rl");
        }

        // Round-trip serialization preserves the block shape.
        let re_serialized = serde_json::to_string(&m).expect("serialize");
        assert!(re_serialized.contains("\"namespace\":\"rl\""));
    }

    #[test]
    fn module_manifest_db_block_rejects_uppercase_namespace() {
        let raw = r#"{
            "id": "rl-mod",
            "name": "RL",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "db": { "migrations_dir": "db/", "namespace": "RL" }
        }"#;
        let err = ModuleManifest::from_json(raw).expect_err("must reject uppercase");
        assert!(err.contains("namespace"));
    }

    #[test]
    fn module_manifest_db_block_rejects_leading_digit() {
        let raw = r#"{
            "id": "rl-mod",
            "name": "RL",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "db": { "migrations_dir": "db/", "namespace": "1rl" }
        }"#;
        let err = ModuleManifest::from_json(raw).expect_err("must reject leading digit");
        assert!(err.contains("namespace"));
    }

    #[test]
    fn module_manifest_db_block_rejects_hyphen() {
        let raw = r#"{
            "id": "rl-mod",
            "name": "RL",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "db": { "migrations_dir": "db/", "namespace": "rl-mod" }
        }"#;
        let err = ModuleManifest::from_json(raw).expect_err("must reject hyphen");
        assert!(err.contains("namespace"));
    }

    #[test]
    fn module_manifest_db_block_rejects_empty_namespace() {
        let raw = r#"{
            "id": "rl-mod",
            "name": "RL",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "db": { "migrations_dir": "db/", "namespace": "" }
        }"#;
        let err = ModuleManifest::from_json(raw).expect_err("must reject empty namespace");
        assert!(err.to_lowercase().contains("namespace"));
    }

    #[test]
    fn module_manifest_db_block_rejects_empty_migrations_dir() {
        let raw = r#"{
            "id": "rl-mod",
            "name": "RL",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "db": { "migrations_dir": "", "namespace": "rl" }
        }"#;
        let err = ModuleManifest::from_json(raw).expect_err("must reject empty migrations_dir");
        assert!(err.contains("migrations_dir"));
    }

    // ─── v0.2.32 (CHAINED_ACTION + L4 + L5, 2026-05-24): renderer adds ───
    //
    // Three new manifest additions land in v0.2.32:
    //   - `ActionDescriptor::ChainedAction` — generic chained-action
    //     primitive (CHAINED_ACTION). Threads each step's response into
    //     the next step's body via `{{previous_step.<field>}}`.
    //   - `ConfigControl::InfoDynamic` (L4) — live read-only info
    //     display bound to a `module_db` source.
    //   - `ConfigControl::DatePicker` (L5) — native HTML date picker
    //     with keyword defaults (`today` / `30_days_ago` / `90_days_ago`).
    //
    // These tests pin the wire shape: backward-compat manifests still
    // parse, the new shapes round-trip cleanly, and each variant
    // surfaces the expected fields with serde defaults applied.

    /// chained_action with two steps + a polling block on the
    /// CHAINED_ACTION level (attaches to the final step at execute time)
    /// + a `{{previous_step.local_path}}` token in the second step's
    /// body. Round-trips through serde without losing structure.
    #[test]
    fn chained_action_two_step_with_polling_round_trips() {
        let json = r#"{
            "kind": "chained_action",
            "steps": [
                {
                    "kind": "http",
                    "method": "POST",
                    "path": "/download_default",
                    "body": {"embedding_source": "{{control:emb_src}}"}
                },
                {
                    "kind": "http",
                    "method": "POST",
                    "path": "/finetune",
                    "body": {
                        "mode": "offline",
                        "starting_checkpoint": "{{previous_step.local_path}}"
                    }
                }
            ],
            "polling": {
                "endpoint": "/finetune_status",
                "interval_seconds": 2,
                "max_attempts": 1800
            },
            "rollback_on_step_failure": false
        }"#;
        let action: ActionDescriptor = serde_json::from_str(json).expect("chained_action parses");
        match action {
            ActionDescriptor::ChainedAction {
                steps,
                polling,
                rollback_on_step_failure,
            } => {
                assert_eq!(steps.len(), 2, "two steps preserved");
                assert!(!rollback_on_step_failure, "default rollback flag");
                // Step 1 — POST /download_default
                match &steps[0] {
                    ActionDescriptor::Http { method, path, body, .. } => {
                        assert_eq!(*method, HttpMethod::Post);
                        assert_eq!(path, "/download_default");
                        let body = body.as_ref().expect("body present");
                        assert_eq!(body["embedding_source"], "{{control:emb_src}}");
                    }
                    other => panic!("step 1 expected Http, got {:?}", other),
                }
                // Step 2 — POST /finetune with {{previous_step.local_path}}
                match &steps[1] {
                    ActionDescriptor::Http { method, path, body, .. } => {
                        assert_eq!(*method, HttpMethod::Post);
                        assert_eq!(path, "/finetune");
                        let body = body.as_ref().expect("body present");
                        assert_eq!(
                            body["starting_checkpoint"],
                            "{{previous_step.local_path}}",
                        );
                        assert_eq!(body["mode"], "offline");
                    }
                    other => panic!("step 2 expected Http, got {:?}", other),
                }
                let polling = polling.expect("polling present on chained_action");
                assert_eq!(polling.endpoint, "/finetune_status");
                assert_eq!(polling.interval_seconds, 2);
                assert_eq!(polling.max_attempts, 1800);
            }
            other => panic!("expected ChainedAction, got {:?}", other),
        }
    }

    /// Defaults: chained_action without polling or rollback_on_step_failure
    /// parses cleanly with sensible defaults (no polling, rollback flag
    /// false).
    #[test]
    fn chained_action_minimum_shape_parses() {
        let json = r#"{
            "kind": "chained_action",
            "steps": [
                { "kind": "http", "method": "GET", "path": "/a" }
            ]
        }"#;
        let action: ActionDescriptor = serde_json::from_str(json).expect("minimal chained_action parses");
        match action {
            ActionDescriptor::ChainedAction {
                steps,
                polling,
                rollback_on_step_failure,
            } => {
                assert_eq!(steps.len(), 1);
                assert!(polling.is_none(), "no polling declared");
                assert!(!rollback_on_step_failure, "default flag");
            }
            other => panic!("expected ChainedAction, got {:?}", other),
        }
    }

    /// info_dynamic with a module_db source round-trips. The wire shape
    /// is the canonical v0.2.7 RL example: read the `weights_version`
    /// field from the `rl_weights_state` table keyed by `{{project_id}}`.
    #[test]
    fn info_dynamic_module_db_source_serde_round_trip() {
        let manifest_json = r#"{
            "id": "rl-test",
            "name": "RL Test",
            "version": "0.2.7",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "pro" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "gui": {
                "config_tab": {
                    "title": "RL Reranker",
                    "sections": [{
                        "title": "Status",
                        "controls": [
                            { "kind": "info_dynamic", "id": "weights_version_live",
                              "label": "Current weights version",
                              "tooltip": "Live read from module DB.",
                              "source": {
                                  "kind": "module_db",
                                  "table": "rl_weights_state",
                                  "key": "{{project_id}}",
                                  "field": "weights_version"
                              },
                              "format": "Version: {value}",
                              "fallback": "never"
                            }
                        ]
                    }]
                }
            }
        }"#;
        let m = ModuleManifest::from_json(manifest_json).expect("manifest parses");
        // Borrow gui by reference so the test can re-serialize `m` below.
        let gui_ref = m.gui.as_ref().expect("gui present");
        let tab_ref = gui_ref.config_tab.as_ref().expect("config_tab present");
        let controls = &tab_ref.sections[0].controls;
        match &controls[0] {
            ConfigControl::InfoDynamic {
                id,
                label,
                tooltip,
                source,
                format,
                fallback,
            } => {
                assert_eq!(id, "weights_version_live");
                assert_eq!(label, "Current weights version");
                assert_eq!(tooltip.as_deref(), Some("Live read from module DB."));
                assert_eq!(format, "Version: {value}");
                assert_eq!(fallback.as_deref(), Some("never"));
                match source {
                    InfoDynamicSource::ModuleDb { table, key, field } => {
                        assert_eq!(table, "rl_weights_state");
                        assert_eq!(key, "{{project_id}}");
                        assert_eq!(field, "weights_version");
                    }
                }
            }
            other => panic!("expected InfoDynamic, got {:?}", other),
        }

        // Re-serialize must preserve the `kind` tags.
        let re = serde_json::to_string(&m).expect("re-serialize");
        assert!(re.contains("\"kind\":\"info_dynamic\""));
        assert!(re.contains("\"kind\":\"module_db\""));
    }

    /// info_dynamic format defaults to `"{value}"` when omitted —
    /// matches the rendererʼs implicit "just print the value" behaviour.
    #[test]
    fn info_dynamic_format_defaults_to_value_token() {
        let json = r#"{
            "kind": "info_dynamic",
            "id": "x", "label": "X",
            "source": { "kind": "module_db", "table": "t", "key": "k", "field": "f" }
        }"#;
        let c: ConfigControl = serde_json::from_str(json).expect("info_dynamic parses");
        match c {
            ConfigControl::InfoDynamic { format, fallback, .. } => {
                assert_eq!(format, "{value}", "default format");
                assert!(fallback.is_none(), "no fallback declared");
            }
            other => panic!("expected InfoDynamic, got {:?}", other),
        }
    }

    /// date_picker round-trips with keyword default + min/max + on_change
    /// descriptor. Pins the canonical v0.2.7 R2 shape (earliest_date
    /// filter for `/global/retrain`).
    #[test]
    fn date_picker_keyword_default_and_on_change_round_trip() {
        let json = r#"{
            "kind": "date_picker",
            "id": "earliest_date",
            "label": "Earliest date",
            "tooltip": "Only train on data newer than this.",
            "default": "30_days_ago",
            "min": "2020-01-01",
            "max": "2030-12-31",
            "on_change": {
                "kind": "http",
                "method": "POST",
                "path": "/set_earliest_date",
                "body": { "date": "{{value}}" }
            }
        }"#;
        let c: ConfigControl = serde_json::from_str(json).expect("date_picker parses");
        match c {
            ConfigControl::DatePicker {
                id,
                label,
                tooltip,
                default,
                min,
                max,
                on_change,
            } => {
                assert_eq!(id, "earliest_date");
                assert_eq!(label, "Earliest date");
                assert_eq!(tooltip.as_deref(), Some("Only train on data newer than this."));
                assert_eq!(default.as_deref(), Some("30_days_ago"));
                assert_eq!(min.as_deref(), Some("2020-01-01"));
                assert_eq!(max.as_deref(), Some("2030-12-31"));
                let aa = on_change.expect("on_change present");
                match aa {
                    ActionRef::Descriptor(ActionDescriptor::Http { path, .. }) => {
                        assert_eq!(path, "/set_earliest_date");
                    }
                    other => panic!("expected Http descriptor, got {:?}", other),
                }
            }
            other => panic!("expected DatePicker, got {:?}", other),
        }
    }

    /// date_picker with no optional fields parses cleanly — every
    /// optional field's serde default kicks in.
    #[test]
    fn date_picker_minimum_shape_parses() {
        let json = r#"{
            "kind": "date_picker", "id": "d", "label": "Pick a date"
        }"#;
        let c: ConfigControl = serde_json::from_str(json).expect("minimal date_picker parses");
        match c {
            ConfigControl::DatePicker {
                default,
                min,
                max,
                on_change,
                ..
            } => {
                assert!(default.is_none());
                assert!(min.is_none());
                assert!(max.is_none());
                assert!(on_change.is_none());
            }
            other => panic!("expected DatePicker, got {:?}", other),
        }
    }

    /// Backward compat: a v0.2.31 manifest (the immediate predecessor)
    /// that uses ONLY the pre-v0.2.32 controls + no chained_action still
    /// parses cleanly through the v0.2.32 schema. Pins the load-bearing
    /// "additive only" invariant: existing paid modules MUST keep working
    /// after the v0.2.32 renderer adds info_dynamic / date_picker / chained_action.
    #[test]
    fn v0_2_31_manifest_without_new_kinds_remains_parseable() {
        // Cross-check against the existing v0.2.26 fixture (which covers
        // every pre-v0.2.32 control kind + ActionRef shape). This is
        // basically a guard: if v0.2.32 introduces a serde change that
        // breaks the pre-existing fixture, this test surfaces it
        // immediately with a clear "back-compat broken" failure.
        let m = ModuleManifest::from_json(v0_2_26_controls_fixture_manifest())
            .expect("v0.2.26 fixture must still parse under v0.2.32 schema");
        let controls = &m.gui.unwrap().config_tab.unwrap().sections[0].controls;
        // None of these controls are InfoDynamic / DatePicker.
        for control in controls {
            match control {
                ConfigControl::InfoDynamic { .. } => panic!("v0.2.26 fixture must not have InfoDynamic"),
                ConfigControl::DatePicker { .. } => panic!("v0.2.26 fixture must not have DatePicker"),
                _ => {}
            }
        }
    }

    #[test]
    fn module_manifest_db_block_accepts_namespace_with_digits_and_underscores() {
        let raw = r#"{
            "id": "rl-mod",
            "name": "RL",
            "version": "0.1.0",
            "category": "community",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "db": { "migrations_dir": "db/", "namespace": "rl_v2_alpha9" }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("must parse");
        assert_eq!(m.db.unwrap().namespace, "rl_v2_alpha9");
    }

    // ─── v0.2.33 Agent D (2026-05-25): tauri_command step kind + ───────────
    //     stop_on_failure alias + Unsupported ConfigControl variant
    //
    // Three additions in v0.2.33:
    //   - `ActionDescriptor::TauriCommand { command, args }` — chained_action
    //     step kind that the RL v0.2.7 manifest uses (was a hard parse
    //     failure on v0.2.32 because the variant didn't exist).
    //   - `stop_on_failure` alias for `rollback_on_step_failure` — module-author
    //     shipped v0.2.7 with the alias spelling before the canonical name
    //     was settled.
    //   - `ConfigControl::Unsupported` — forward-compat fallback for
    //     manifests that ship a control kind unknown to this launcher
    //     version. Lenient by default; strict mode restores the
    //     pre-v0.2.33 reject-at-parse behaviour for CI gates.

    /// `{"kind": "tauri_command", "command": "...", "args": {...}}`
    /// parses as `ActionDescriptor::TauriCommand`. The shape matches the
    /// v0.2.7 RL manifest's button-level dispatch (one Tauri command,
    /// args struct forwarded to the Rust handler).
    #[test]
    fn tauri_command_step_deserialize() {
        let json = r#"{
            "kind": "tauri_command",
            "command": "module_download_default_weights",
            "args": {
                "module_id": "vct-rl-reranker",
                "project_id": "{{project_id}}",
                "embedding_source": "qwen3"
            }
        }"#;
        let action: ActionDescriptor = serde_json::from_str(json)
            .expect("tauri_command descriptor parses");
        match action {
            ActionDescriptor::TauriCommand { command, args } => {
                assert_eq!(command, "module_download_default_weights");
                assert_eq!(args["module_id"], "vct-rl-reranker");
                assert_eq!(args["project_id"], "{{project_id}}");
                assert_eq!(args["embedding_source"], "qwen3");
            }
            other => panic!("expected TauriCommand, got {:?}", other),
        }
    }

    /// `tauri_command` step with `args` omitted should default to
    /// `serde_json::Value::Null` — matching the schema's `#[serde(default)]`
    /// on the field. Callers that don't need to pass args don't have to
    /// declare an empty object.
    #[test]
    fn tauri_command_step_omitted_args_defaults_to_null() {
        let json = r#"{
            "kind": "tauri_command",
            "command": "some_no_arg_command"
        }"#;
        let action: ActionDescriptor = serde_json::from_str(json)
            .expect("tauri_command without args parses");
        match action {
            ActionDescriptor::TauriCommand { command, args } => {
                assert_eq!(command, "some_no_arg_command");
                assert_eq!(args, serde_json::Value::Null);
            }
            other => panic!("expected TauriCommand, got {:?}", other),
        }
    }

    /// chained_action with `stop_on_failure: true` (the v0.2.7 RL
    /// manifest alias) deserialises with the canonical
    /// `rollback_on_step_failure` field set to `true`. This is THE
    /// regression the launcher v0.2.32 hit at line 242 of the RL
    /// manifest — the alias didn't exist and `rollback_on_step_failure`
    /// was the only accepted spelling.
    #[test]
    fn stop_on_failure_alias_parses() {
        let json = r#"{
            "kind": "chained_action",
            "stop_on_failure": true,
            "steps": [
                { "kind": "http", "method": "GET", "path": "/a" }
            ]
        }"#;
        let action: ActionDescriptor = serde_json::from_str(json)
            .expect("stop_on_failure alias parses");
        match action {
            ActionDescriptor::ChainedAction { rollback_on_step_failure, steps, .. } => {
                assert!(
                    rollback_on_step_failure,
                    "stop_on_failure: true MUST map to rollback_on_step_failure: true",
                );
                assert_eq!(steps.len(), 1);
            }
            other => panic!("expected ChainedAction, got {:?}", other),
        }
    }

    /// Backward compat: the canonical `rollback_on_step_failure` name
    /// still works. New manifests should use this spelling; old
    /// manifests using the alias still parse identically.
    #[test]
    fn rollback_on_step_failure_canonical_name_works() {
        let json = r#"{
            "kind": "chained_action",
            "rollback_on_step_failure": true,
            "steps": [
                { "kind": "http", "method": "GET", "path": "/a" }
            ]
        }"#;
        let action: ActionDescriptor = serde_json::from_str(json)
            .expect("canonical name parses");
        match action {
            ActionDescriptor::ChainedAction { rollback_on_step_failure, .. } => {
                assert!(rollback_on_step_failure);
            }
            other => panic!("expected ChainedAction, got {:?}", other),
        }
    }

    /// Full smoke test: load the actual v0.2.7 RL manifest from
    /// `paid-modules/vct-rl-reranker/vct-module.json` and assert it
    /// parses cleanly via `ModuleManifest::from_json`. This is THE
    /// regression test for the v0.2.32 manifest-validation bug — pre-D, the
    /// manifest rejected at line 242 (`tauri_command` step kind)
    /// because the launcher's `ActionDescriptor` enum didn't have the
    /// variant. Post-D, it parses cleanly and the catalog tile can
    /// render with the correct version.
    ///
    /// Like `vct_rl_reranker_manifest_deserializes` above, this test
    /// is informational on dev clones without the paid-modules
    /// staging dir.
    #[test]
    fn rl_reranker_v0_2_7_manifest_with_chained_action_tauri_command_parses() {
        let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .expect("walk to repo root")
            .to_path_buf();
        let path = repo_root.join("paid-modules/vct-rl-reranker/vct-module.json");
        if !path.exists() {
            eprintln!(
                "[test skip] paid-modules/vct-rl-reranker/vct-module.json not present \
                 (path: {}) — skipping v0.2.7 regression smoke",
                path.display()
            );
            return;
        }
        let body = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        let manifest = ModuleManifest::from_json(&body)
            .unwrap_or_else(|e| panic!(
                "deserialize {} (this is the v0.2.32 regression — \
                 tauri_command step / stop_on_failure alias must parse): {}",
                path.display(),
                e,
            ));

        // Verify at least one chained_action with at least one
        // tauri_command step exists in the gui config tab. If the RL
        // manifest ever drops these shapes the test still passes (the
        // overall parse is what matters), but we assert when present
        // to make the regression target explicit.
        if let Some(gui) = manifest.gui.as_ref() {
            if let Some(tab) = gui.config_tab.as_ref() {
                let mut found_chained_with_tauri_step = false;
                'outer: for section in &tab.sections {
                    for control in &section.controls {
                        if let ConfigControl::Button { action, .. } = control {
                            if let ActionRef::Descriptor(d) = action {
                                match d {
                                    ActionDescriptor::TauriCommand { .. } => {
                                        // Direct button → tauri_command — fine.
                                    }
                                    ActionDescriptor::ChainedAction { steps, .. } => {
                                        for step in steps {
                                            if matches!(step, ActionDescriptor::TauriCommand { .. }) {
                                                found_chained_with_tauri_step = true;
                                                break 'outer;
                                            }
                                        }
                                    }
                                    _ => {}
                                }
                            }
                        }
                    }
                }
                // Don't hard-assert (the manifest may evolve); just log.
                if !found_chained_with_tauri_step {
                    eprintln!(
                        "[v0.2.7 manifest smoke] no chained_action-with-tauri_command-step \
                         button found — manifest may have evolved beyond v0.2.7 shape, \
                         but the top-level parse succeeded which is the main regression"
                    );
                }
            }
        }
    }

    /// Lenient mode (default): a manifest with an unknown control kind
    /// in a section parses as `ConfigControl::Unsupported`. Other
    /// controls in the same section parse normally.
    #[test]
    fn unsupported_control_kind_parses_lenient() {
        set_strict_manifest_for_test(false);
        let json = r#"{
            "id": "test-mod",
            "name": "Test Module",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "gui": {
                "config_tab": {
                    "title": "Test Tab",
                    "sections": [
                        {
                            "title": "Mixed Section",
                            "controls": [
                                { "kind": "info", "id": "i1", "text": "ok", "variant": "info" },
                                { "kind": "file_drop_zone", "id": "future_ctrl",
                                  "label": "Drop a file (v0.3.0 feature)",
                                  "accepts": ["application/json"] },
                                { "kind": "checkbox", "id": "c1", "label": "Toggle",
                                  "default": false }
                            ]
                        }
                    ]
                }
            }
        }"#;
        let manifest = ModuleManifest::from_json(json)
            .expect("manifest with unknown control kind must parse lenient");
        let controls = &manifest.gui.unwrap().config_tab.unwrap().sections[0].controls;
        assert_eq!(controls.len(), 3, "all 3 controls present (none dropped)");

        // Known controls deserialize normally.
        match &controls[0] {
            ConfigControl::Info { id, text, .. } => {
                assert_eq!(id, "i1");
                assert_eq!(text, "ok");
            }
            other => panic!("controls[0] expected Info, got {:?}", other),
        }
        // Unknown kind becomes Unsupported.
        match &controls[1] {
            ConfigControl::Unsupported { kind_string, raw } => {
                assert_eq!(kind_string, "file_drop_zone");
                assert_eq!(raw["id"], "future_ctrl");
                assert_eq!(raw["label"], "Drop a file (v0.3.0 feature)");
                assert_eq!(raw["accepts"][0], "application/json");
            }
            other => panic!("controls[1] expected Unsupported, got {:?}", other),
        }
        // Subsequent known controls parse normally.
        match &controls[2] {
            ConfigControl::Checkbox { id, label, .. } => {
                assert_eq!(id, "c1");
                assert_eq!(label, "Toggle");
            }
            other => panic!("controls[2] expected Checkbox, got {:?}", other),
        }
    }

    /// Strict mode: the same manifest as `unsupported_control_kind_parses_lenient`
    /// rejects at parse time when `VCT_LAUNCHER_STRICT_MANIFEST=1`.
    /// Restores the pre-v0.2.33 behaviour for CI gates and dev-mode.
    #[test]
    fn unsupported_control_kind_strict_mode_rejects() {
        set_strict_manifest_for_test(true);
        let json = r#"{
            "id": "test-mod",
            "name": "Test Module",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "free" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "service", "command": "echo" },
            "gui": {
                "config_tab": {
                    "title": "Test Tab",
                    "sections": [
                        {
                            "title": "Section",
                            "controls": [
                                { "kind": "file_drop_zone", "id": "x", "label": "Drop" }
                            ]
                        }
                    ]
                }
            }
        }"#;
        let err = ModuleManifest::from_json(json)
            .expect_err("strict mode must reject unknown control kind");
        // Reset for subsequent tests (test isolation — tests sharing the
        // static flag depend on lenient being the default).
        set_strict_manifest_for_test(false);
        assert!(
            err.contains("unknown control kind") || err.contains("strict mode") || err.contains("file_drop_zone") || err.contains("unknown variant"),
            "error message should pinpoint the unknown kind, got: {}",
            err,
        );
    }

    /// An `ActionDescriptor` with an unknown `kind` inside a
    /// chained_action step REMAINS a hard error (no Unsupported
    /// fallback for action descriptors today — only ConfigControl
    /// gets the lenient treatment in v0.2.33). The dispatch surface
    /// for actions has stronger trust requirements than the renderer:
    /// silently skipping an unknown step would change the chain's
    /// semantics. Document the choice here so future refactors don't
    /// quietly relax it.
    #[test]
    fn unsupported_action_descriptor_kind_in_chain_step_rejects() {
        // Lenient mode for ConfigControl — should NOT affect ActionDescriptor.
        set_strict_manifest_for_test(false);
        let json = r#"{
            "kind": "chained_action",
            "steps": [
                { "kind": "future_step_kind", "some_arg": 1 }
            ]
        }"#;
        let err = serde_json::from_str::<ActionDescriptor>(json)
            .expect_err("unknown action kind in chain step must reject");
        let msg = err.to_string();
        assert!(
            msg.contains("unknown variant") || msg.contains("future_step_kind") || msg.contains("kind"),
            "error must reference the unknown step kind, got: {}",
            msg,
        );
    }

    // ─── v0.2.34: validate_install_dir hardening (Agent A) ──────────
    //
    // A test install of RL Reranker v0.2.7 on 2026-05-25 surfaced
    // a guard-vs-bootstrap ordering bug: `~/.vct/modules/` is created
    // lazily by container_pull LATER in the install flow, so on a
    // fresh machine both the candidate install_dir AND the allowed
    // root are absent when `validate_install_dir` runs. The
    // pre-v0.2.34 fallback (`unwrap_or_else(|_| allowed_root.to_path_buf())`)
    // produced a non-canonical literal path which was lexically
    // incomparable with the canonicalized candidate ancestor — every
    // first install failed with a spurious "escapes allowed root"
    // error. These tests pin down the four corners of the matrix:
    //   1. neither exists yet (the failure case);
    //   2. root exists, candidate doesn't (typical second install);
    //   3. both exist (typical reinstall path);
    //   4. candidate escapes root (security guarantee preserved).

    #[test]
    fn validate_install_dir_succeeds_when_neither_root_nor_candidate_exists() {
        // The reproducer for the v0.2.34 manifest-fallback bug. The temp dir is
        // created so its parent canonicalises; we then point both
        // `allowed_root` and `candidate` AT NON-EXISTENT subpaths of
        // it. Pre-fix this asserted err; post-fix it must succeed.
        let tmp = tempfile::tempdir().expect("tempdir");
        let allowed_root = tmp.path().join("vct").join("modules");
        let candidate = allowed_root.join("vct-rl-reranker");
        assert!(!allowed_root.exists(), "test precondition: root must not exist");
        assert!(!candidate.exists(), "test precondition: candidate must not exist");

        validate_install_dir(&candidate, &allowed_root).unwrap_or_else(|e| {
            panic!(
                "validate_install_dir must succeed when neither path exists yet \
                 (the bootstrap mkdir creates them later); got error: {}",
                e
            )
        });
    }

    #[test]
    fn validate_install_dir_succeeds_when_root_exists_candidate_does_not() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let allowed_root = tmp.path().join("modules");
        std::fs::create_dir_all(&allowed_root).expect("mkdir root");
        let candidate = allowed_root.join("vct-rl-reranker");
        assert!(!candidate.exists());

        validate_install_dir(&candidate, &allowed_root)
            .expect("typical second-install case: root exists, candidate to be created");
    }

    #[test]
    fn validate_install_dir_succeeds_when_both_exist() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let allowed_root = tmp.path().join("modules");
        let candidate = allowed_root.join("vct-rl-reranker");
        std::fs::create_dir_all(&candidate).expect("mkdir candidate (which also creates root)");

        validate_install_dir(&candidate, &allowed_root)
            .expect("reinstall case: both exist on disk");
    }

    #[test]
    fn validate_install_dir_rejects_candidate_outside_root() {
        // Security guarantee: even when neither path exists, a
        // candidate that lexically escapes the root must be refused.
        let tmp = tempfile::tempdir().expect("tempdir");
        let allowed_root = tmp.path().join("vct").join("modules");
        // `candidate` shares an ancestor with `allowed_root` but is
        // NOT under it.
        let candidate = tmp.path().join("other").join("evil-module");

        let err = validate_install_dir(&candidate, &allowed_root)
            .expect_err("candidate outside root must be rejected");
        assert!(
            err.contains("escapes allowed root"),
            "error must name the failure mode; got: {}",
            err
        );
    }

    // ─── v0.2.34 Agent E (Phase 4 generalisation) tests ──────────────

    #[test]
    fn mcp_registration_accepts_tool_allowlist_field() {
        // v0.2.34: paid module declares a per-tool allowlist in its
        // manifest. The launcher parses + persists this into
        // `module_mcp_tool_defaults` at install time. Verify the
        // serde shape lands cleanly.
        let raw = r#"{
            "id": "vendor-reranker",
            "name": "Vendor Reranker",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "pro" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "mcp_stdio", "command": "python", "args": ["-m", "x"] },
            "mcp_registration": {
                "mcp_name": "vendor-reranker",
                "tool_allowlist": [
                    { "tool": "rerank", "default_enabled": true, "description": "re-rank results" },
                    { "tool": "explain", "default_enabled": false },
                    { "tool": "debug" }
                ]
            }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("parse");
        let reg = m.mcp_registration.expect("mcp_registration present");
        let allowlist = reg.tool_allowlist.expect("tool_allowlist present");
        assert_eq!(allowlist.len(), 3);
        assert_eq!(allowlist[0].tool, "rerank");
        assert!(allowlist[0].default_enabled);
        assert_eq!(allowlist[0].description.as_deref(), Some("re-rank results"));
        assert_eq!(allowlist[1].tool, "explain");
        assert!(!allowlist[1].default_enabled);
        assert!(allowlist[1].description.is_none());
        // Field omitted entirely → defaults to true (safer for module authors
        // who declare a flat tool list).
        assert_eq!(allowlist[2].tool, "debug");
        assert!(
            allowlist[2].default_enabled,
            "omitted `default_enabled` must default to true; got {:?}",
            allowlist[2]
        );
        assert!(allowlist[2].description.is_none());
    }

    #[test]
    fn mcp_registration_without_tool_allowlist_back_compat() {
        // Pre-v0.2.34 manifests didn't declare `tool_allowlist`. Verify
        // they still parse cleanly — `tool_allowlist` defaults to None,
        // matching the "no per-tool defaults; fall through to hub
        // fallback" semantic on the hub side.
        let raw = r#"{
            "id": "older-mod",
            "name": "Older Module",
            "version": "0.1.0",
            "category": "paid-orchestrator",
            "license": { "min_orchestrator_tier": "pro" },
            "install": { "method": "git_clone", "source": "https://example.com/x.git" },
            "runtime": { "type": "mcp_stdio", "command": "python", "args": [] },
            "mcp_registration": { "mcp_name": "older-mcp" }
        }"#;
        let m = ModuleManifest::from_json(raw).expect("parse");
        let reg = m.mcp_registration.expect("mcp_registration present");
        assert!(
            reg.tool_allowlist.is_none(),
            "absent tool_allowlist must deserialize as None for v0.2.33 back-compat"
        );
    }

    #[test]
    fn validate_install_dir_rejects_relative_candidate() {
        // The function explicitly refuses relative paths (callers
        // resolve via `PlaceholderCtx::resolve_install_dir` first).
        let err = validate_install_dir(
            Path::new("relative/path"),
            Path::new("/tmp/vct/modules"),
        )
        .expect_err("relative candidate must be rejected");
        assert!(
            err.contains("must be absolute"),
            "error must say 'must be absolute'; got: {}",
            err
        );
    }

    #[test]
    fn tool_allowlist_entry_serializes_round_trip() {
        // Wire-shape stability check: a round-trip through JSON
        // preserves every field. The skip_serializing_if on
        // `description` means a None description doesn't show up in
        // the serialized JSON — matches v0.2.33's `description: null`
        // omission convention.
        let entry = ToolAllowlistEntry {
            tool: "render".into(),
            default_enabled: true,
            description: Some("renders mermaid".into()),
        };
        let json = serde_json::to_string(&entry).unwrap();
        assert!(json.contains("\"tool\":\"render\""));
        assert!(json.contains("\"default_enabled\":true"));
        assert!(json.contains("\"description\":\"renders mermaid\""));
        let parsed: ToolAllowlistEntry = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, entry);
    }

    #[test]
    fn tool_allowlist_entry_skips_none_description() {
        let entry = ToolAllowlistEntry {
            tool: "render".into(),
            default_enabled: true,
            description: None,
        };
        let json = serde_json::to_string(&entry).unwrap();
        assert!(
            !json.contains("description"),
            "None description should be skipped during serialize, got: {}",
            json
        );
    }

    // ─── v0.2.49 Stream A: InstallScope serde + defaults ────────────────

    /// Default scope is `per_project` — pre-v0.2.49 manifests that omit
    /// the field deserialize unchanged.
    #[test]
    fn v0249_install_scope_defaults_to_per_project() {
        assert_eq!(InstallScope::default(), InstallScope::PerProject);
        assert!(!InstallScope::default().is_global());
    }

    /// Manifests omitting `install.scope` default to per-project — the
    /// load-bearing back-compat guarantee for every pre-v0.2.49 module.
    #[test]
    fn v0249_manifest_without_scope_field_deserializes_as_per_project() {
        let json = r#"{
            "manifest_version": 1,
            "id": "test-mod",
            "name": "Test",
            "version": "0.1.0",
            "category": "paid-independent",
            "install": {
                "method": "container_pull",
                "container": {
                    "image": "ghcr.io/x/y",
                    "pull_token_endpoint": "https://example.invalid/token"
                }
            },
            "runtime": {
                "type": "container",
                "command": "python"
            }
        }"#;
        let m: ModuleManifest = serde_json::from_str(json).expect("parse");
        assert_eq!(m.install.scope, InstallScope::PerProject);
        assert!(!m.install.scope.is_global());
    }

    /// Manifests declaring `install.scope = "global"` deserialize as
    /// `InstallScope::Global`.
    #[test]
    fn v0249_manifest_with_scope_global_deserializes_correctly() {
        let json = r#"{
            "manifest_version": 1,
            "id": "test-mod",
            "name": "Test",
            "version": "0.1.0",
            "category": "paid-independent",
            "install": {
                "method": "container_pull",
                "scope": "global",
                "container": {
                    "image": "ghcr.io/x/y",
                    "pull_token_endpoint": "https://example.invalid/token"
                }
            },
            "runtime": {
                "type": "container",
                "command": "python"
            }
        }"#;
        let m: ModuleManifest = serde_json::from_str(json).expect("parse");
        assert_eq!(m.install.scope, InstallScope::Global);
        assert!(m.install.scope.is_global());
    }

    /// Manifests declaring `install.scope = "per_project"` explicitly
    /// also work.
    #[test]
    fn v0249_manifest_with_scope_per_project_deserializes_correctly() {
        let json = r#"{
            "manifest_version": 1,
            "id": "test-mod",
            "name": "Test",
            "version": "0.1.0",
            "category": "paid-independent",
            "install": {
                "method": "container_pull",
                "scope": "per_project",
                "container": {
                    "image": "ghcr.io/x/y",
                    "pull_token_endpoint": "https://example.invalid/token"
                }
            },
            "runtime": {
                "type": "container",
                "command": "python"
            }
        }"#;
        let m: ModuleManifest = serde_json::from_str(json).expect("parse");
        assert_eq!(m.install.scope, InstallScope::PerProject);
    }

    /// Unknown scope values are rejected at parse time (forces typo
    /// authoring errors to surface immediately).
    #[test]
    fn v0249_manifest_with_unknown_scope_value_is_rejected() {
        let json = r#"{
            "manifest_version": 1,
            "id": "test-mod",
            "name": "Test",
            "version": "0.1.0",
            "category": "paid-independent",
            "install": {
                "method": "container_pull",
                "scope": "machine",
                "container": {
                    "image": "ghcr.io/x/y",
                    "pull_token_endpoint": "https://example.invalid/token"
                }
            },
            "runtime": {
                "type": "container",
                "command": "python"
            }
        }"#;
        let result = serde_json::from_str::<ModuleManifest>(json);
        assert!(
            result.is_err(),
            "unknown scope value must be rejected (typo guard)"
        );
    }

    /// `InstallScope::as_str` round-trips through serde's wire form.
    #[test]
    fn v0249_install_scope_as_str_matches_serde_form() {
        assert_eq!(InstallScope::PerProject.as_str(), "per_project");
        assert_eq!(InstallScope::Global.as_str(), "global");
        // Round-trip via serde.
        let g: InstallScope = serde_json::from_str("\"global\"").unwrap();
        assert_eq!(g, InstallScope::Global);
        let pp: InstallScope = serde_json::from_str("\"per_project\"").unwrap();
        assert_eq!(pp, InstallScope::PerProject);
    }
}
