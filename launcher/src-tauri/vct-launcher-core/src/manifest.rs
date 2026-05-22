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

use serde::{Deserialize, Serialize};

// ─── Top-level manifest type ────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
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
#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
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
#[derive(Debug, Clone, Serialize, Deserialize)]
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
    #[serde(rename = "multi_select")]
    MultiSelect {
        id: String,
        label: String,
        #[serde(default)]
        tooltip: Option<String>,
        /// Tauri command name OR structured descriptor returning
        /// `Vec<{value, label}>`.
        options_source: ActionRef,
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
}

fn default_link_target() -> String {
    "external".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SelectOption {
    pub value: String,
    pub label: String,
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
#[derive(Debug, Clone, Serialize, Deserialize)]
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
/// v1 ships ONE kind: `http`. Future kinds (e.g. `shell` for sandboxed
/// subprocess actions) are intentionally NOT included — they would
/// expand the trust surface significantly and need their own design
/// pass.
#[derive(Debug, Clone, Serialize, Deserialize)]
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
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
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
#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "kebab-case")]
pub enum ModuleCategory {
    Core,
    PaidOrchestrator,
    PaidIndependent,
    Community,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Compatibility {
    #[serde(default = "default_hosts")]
    pub hosts: Vec<String>,
    #[serde(default)]
    pub min_launcher_version: Option<String>,
}
fn default_hosts() -> Vec<String> {
    vec!["base".into(), "mao".into()]
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
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

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
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
}

/// Container-pull install metadata. Carries the registry image reference
/// + the signed-URL token gateway endpoint. The launcher's installer
/// engine POSTs the user's validated-tier JWT to `pull_token_endpoint`
/// before invoking `podman/docker pull` — no anonymous registry access
/// is ever attempted.
#[derive(Debug, Clone, Serialize, Deserialize)]
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
    /// once per day per VCO_dev's locked decision (2026-05-16).
    #[serde(default)]
    pub rotate_weights_endpoint: Option<String>,
}

fn default_pull_token_method() -> String {
    "POST".into()
}
fn default_install_dir() -> String {
    "{VCT_MODULES}/{MODULE_ID}".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeBlock {
    pub r#type: String, // "mcp_stdio" | "mcp_http" | "service" | "cli" | "container"
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
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
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
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
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
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VolumeMount {
    pub host: String,
    pub container: String,
    #[serde(default)]
    pub mode: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpRegistration {
    #[serde(default = "default_true")]
    pub enabled_by_default: bool,
    pub mcp_name: String,
    #[serde(default = "default_target_all")]
    pub target_projects: serde_json::Value, // "all" | "none" | ["path"]
    #[serde(default = "default_user_scope")]
    pub scope: String, // "user" | "project"
}
fn default_target_all() -> serde_json::Value {
    serde_json::Value::String("all".into())
}
fn default_user_scope() -> String {
    "user".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
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

        Ok(m)
    }

    /// Returns true if this manifest is installable on the given host.
    pub fn is_compatible_with_host(&self, host: &str) -> bool {
        self.compatibility.hosts.iter().any(|h| h == host)
    }
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
pub fn validate_install_dir(candidate: &Path, allowed_root: &Path) -> Result<(), String> {
    let abs = if candidate.is_absolute() {
        candidate.to_path_buf()
    } else {
        return Err(format!("install_dir must be absolute: {}", candidate.display()));
    };

    // If the exact path doesn't exist yet, canonicalize the closest existing
    // ancestor (avoid the "directory doesn't exist" canonicalize failure).
    let mut probe = abs.as_path();
    let canonical_base = loop {
        match probe.canonicalize() {
            Ok(p) => break p,
            Err(_) => match probe.parent() {
                Some(p) => probe = p,
                None => return Err("install_dir has no canonicalizable ancestor".into()),
            },
        }
    };
    let canonical_root = allowed_root
        .canonicalize()
        .unwrap_or_else(|_| allowed_root.to_path_buf());

    if !canonical_base.starts_with(&canonical_root) {
        return Err(format!(
            "install_dir {} escapes allowed root {}",
            candidate.display(),
            allowed_root.display()
        ));
    }
    Ok(())
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
        // G3 (v0.2.22): bumped to 0.1.1 — the version actually released on
        // GHCR (`ghcr.io/hotak92/vct-rl-reranker:0.1.1-{cpu,cuda,rocm}`) and
        // the version that `runtime.args` + `gpu_image_variants` already
        // pin. Pre-v0.2.22 the manifest top-level was 0.1.2 (an unreleased
        // bump), while this test still asserted 0.1.0 — both stale.
        assert_eq!(manifest.version, "0.1.1");
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
            ConfigControl::MultiSelect { id, options_source, tooltip, .. } => {
                assert_eq!(id, "ms1");
                assert_eq!(action_ref_as_legacy(options_source), "list_options");
                assert!(tooltip.is_some(), "tooltip declared in fixture");
            }
            other => panic!("expected MultiSelect, got {:?}", other),
        }
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
        // kind exists. Info is required (section 1 is status-only).
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
                    ConfigControl::Info { .. } => has_info = true,
                    ConfigControl::Select { .. } => {}
                    // v0.2.26+ kinds — not exercised by the current
                    // vct-rl-reranker manifest, but acknowledged here
                    // so a future RL manifest update that adds one of
                    // them doesn't unexpectedly fail this test.
                    ConfigControl::TextInput { .. }
                    | ConfigControl::NumberInput { .. }
                    | ConfigControl::StatusDisplay { .. }
                    | ConfigControl::FilePicker { .. }
                    | ConfigControl::Link { .. } => {}
                }
            }
        }
        assert!(has_checkbox, "manifest must declare at least one checkbox");
        assert!(has_button, "manifest must declare at least one button");
        assert!(has_multi_select, "manifest must declare a multi_select");
        assert!(has_info, "section 1 must include at least one info banner");
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
        let ActionDescriptor::Http { next_action, .. } = action;
        let inner = next_action.expect("level 2 present");
        let ActionDescriptor::Http { next_action, path, .. } = *inner;
        assert_eq!(path, "/step2");
        let deepest = next_action.expect("level 3 present");
        let ActionDescriptor::Http { method, path, next_action, .. } = *deepest;
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
                        let ActionDescriptor::Http { path, polling, .. } = next.as_ref();
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
}
