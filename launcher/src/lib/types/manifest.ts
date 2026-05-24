// TypeScript mirror of the Rust manifest schema in
// `launcher/src-tauri/vct-launcher-core/src/manifest.rs`.
//
// v0.2.26 adds five new control kinds (text_input, number_input,
// status_display, file_picker, link) plus the ActionRef / ActionDescriptor
// dispatcher contract. The Rust types are the source of truth; this file
// exists so the Svelte 5 renderer can type-check the schema it receives
// over the Tauri bridge.
//
// Adding a new variant requires:
//   1. New variant in `manifest::ConfigControl` (Rust)
//   2. New TS variant here
//   3. New case in `ModuleConfigTab.svelte`'s renderControl dispatch
//   4. Doc update on both sides.

// ─── ActionRef + ActionDescriptor (v0.2.26 dispatcher contract) ────────

/**
 * Polling spec for long-running actions. When attached to an
 * `ActionDescriptor` of kind `http`, the Rust dispatcher kicks the
 * request, then re-hits `endpoint` every `interval_seconds` until
 * a terminal state is reached or `max_attempts` is exceeded.
 *
 * Each poll tick fires `progress_event`; terminal failure fires
 * `failed_event`. The renderer subscribes to both via `listen()`.
 */
export interface PollingSpec {
  /** Container-relative URL for the polling GET, e.g. `/finetune_status`. */
  endpoint: string;
  /** JSONPath into the kick response that locates the job id. Default `$.job_id`. */
  job_id_path?: string;
  /** Query-parameter name the poller uses to pass the job id back. Default `job_id`. */
  job_id_query_param?: string;
  /** Polling interval. Default 5 s. */
  interval_seconds?: number;
  /** Hard cap on poll ticks. Default 60. */
  max_attempts?: number;
  /** JSONPath into the poll response locating the terminal-state field. Default `$.state`. */
  terminal_state_field?: string;
  /** State values that count as "done". Default `["done"]`. */
  terminal_success_values?: string[];
  /** State values that count as "failed". Default `["failed", "error"]`. */
  terminal_failure_values?: string[];
  /** Tauri event emitted on each poll tick. Default `module://action-progress`. */
  progress_event?: string;
  /** Tauri event emitted on terminal failure. Default `module://action-failed`. */
  failed_event?: string;
}

/** HTTP action descriptor — issues a request to the module's container. */
export interface HttpActionDescriptor {
  kind: 'http';
  method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  path: string;
  body?: unknown;
  polling?: PollingSpec | null;
  /** Chain a follow-up action on success. Bounded by `max_chain_steps` (default 1024). */
  next_action?: ActionDescriptor | null;
}

/**
 * v0.2.32 (CHAINED_ACTION, 2026-05-24): generic chained-action
 * primitive. Executes `steps` serially; each step's response body is
 * threaded into the next step's body via `{{previous_step.<field>}}`
 * or `{{step.N.<field>}}` placeholders. Optional `polling` attaches
 * to the FINAL step.
 *
 * Failure semantics for v0.2.32: on any step failure, the dispatcher
 * logs + propagates the error to the renderer. Previous steps' side
 * effects are NOT rolled back. `rollback_on_step_failure` is reserved
 * for v0.2.33+ (no effect today).
 *
 * v0.2.33 (Agent D, 2026-05-25): the canonical field name is
 * `rollback_on_step_failure`. The Rust schema also accepts
 * `stop_on_failure` as a deserialise alias for back-compat with the
 * v0.2.7 RL manifest, which shipped that spelling. New manifests
 * should use the canonical name; the alias logs a deprecation
 * notice when used.
 */
export interface ChainedActionDescriptor {
  kind: 'chained_action';
  /** Ordered list of step descriptors. Empty → error at dispatch time. */
  steps: ActionDescriptor[];
  /** Polling block attached to the final step's response. */
  polling?: PollingSpec | null;
  /** Reserved for v0.2.33+. Parses but has no effect in v0.2.32. */
  rollback_on_step_failure?: boolean;
  /** v0.2.33: deserialise-only alias for `rollback_on_step_failure`.
   *  Present on the TS type so manifests that ship the alias
   *  type-check correctly in the renderer's snapshot. The Rust
   *  schema collapses both to `rollback_on_step_failure` at parse
   *  time. */
  stop_on_failure?: boolean;
}

/**
 * v0.2.33 (Agent D, 2026-05-25): tauri_command step kind. Invokes a
 * launcher-registered Tauri command by name from inside a
 * chained_action step. The Rust dispatcher consults a strict
 * whitelist before invoking — any name starting with `module_` is
 * allowed, plus an explicit allowlist of legacy non-`module_*`
 * commands (see `MANIFEST_DISPATCHABLE_COMMANDS` in
 * `module_dispatch.rs`).
 *
 * Used by the v0.2.7 RL manifest's "Download default weights" /
 * "Download default + offline pass on top" buttons.
 */
export interface TauriCommandActionDescriptor {
  kind: 'tauri_command';
  /** Tauri command name. Must match the whitelist. */
  command: string;
  /** Args forwarded to the underlying Rust function. Passes through
   *  the dispatcher's placeholder substitution pipeline
   *  (`{{previous_step.<field>}}`, `{{control:<id>}}`, etc.). */
  args?: unknown;
}

/** Declarative action that the generic `module_dispatch_action` Tauri command executes. */
export type ActionDescriptor =
  | HttpActionDescriptor
  | ChainedActionDescriptor
  | TauriCommandActionDescriptor;

/**
 * Either a legacy Tauri command name (string) OR a structured action
 * descriptor (object). The renderer dispatches accordingly via the
 * `dispatchAction` helper in `ModuleConfigTab.svelte`.
 */
export type ActionRef = string | ActionDescriptor;

// ─── ConfigControl variants ─────────────────────────────────────────────

export interface CheckboxControl {
  kind: 'checkbox';
  id: string;
  label: string;
  tooltip?: string | null;
  default?: boolean;
  on_change?: ActionRef | null;
}

/**
 * v0.2.32 L6: runtime-driven filter for `multi_select` options.
 *
 * `kind: "match"` hides options whose `meta.<meta_field>` doesn't
 * equal the runtime-resolved value of `equals_runtime`. v1 supports
 * `equals_runtime: "container.active_embedding"` only; unknown
 * identifiers MUST fall back to "no filtering" (show all options) —
 * the renderer never panics on unrecognised runtime keys.
 *
 * Future filter kinds (regex, range, contains) land as new union
 * variants on this type.
 */
export type MultiSelectFilter = {
  kind: 'match';
  meta_field: string;
  equals_runtime: string;
};

export interface MultiSelectControl {
  kind: 'multi_select';
  id: string;
  label: string;
  tooltip?: string | null;
  options_source: ActionRef;
  /** v0.2.32 L6: runtime-driven option filter. Omit ⇒ all options visible. */
  filter?: MultiSelectFilter | null;
  on_change?: ActionRef | null;
}

/**
 * v0.2.32 L6 mirror of Rust's `SelectOption`. Back-compat: callers
 * that return bare strings get those mapped to
 * `{ value, label: value, badge: undefined, meta: undefined }` on the
 * Rust side BEFORE crossing the Tauri bridge — so by the time JS sees
 * a `SelectOption`, the rich shape is normalised.
 *
 * `badge` is an optional pill rendered next to the label (e.g. "new").
 * `meta` carries opaque per-option metadata that `MultiSelectFilter`
 * predicates match against (e.g. `{embedding_source: "qwen3"}`).
 */
export interface SelectOption {
  value: string;
  label: string;
  badge?: string | null;
  /** Opaque per-option metadata. Top-level keys are looked up by `MultiSelectFilter`. */
  meta?: Record<string, unknown> | null;
}

export interface ButtonControl {
  kind: 'button';
  id: string;
  label: string;
  tooltip?: string | null;
  action: ActionRef;
  variant?: 'primary' | 'secondary' | 'danger' | string | null;
  confirm?: string | null;
}

export interface SelectControl {
  kind: 'select';
  id: string;
  label: string;
  tooltip?: string | null;
  options: { value: string; label: string }[];
  default?: string | null;
  on_change?: ActionRef | null;
}

export interface InfoControl {
  kind: 'info';
  id: string;
  text: string;
  variant?: 'info' | 'warning' | string | null;
}

// ─── v0.2.26 new control kinds ──────────────────────────────────────────

export interface TextInputControl {
  kind: 'text_input';
  id: string;
  label: string;
  tooltip?: string | null;
  default?: string;
  placeholder?: string | null;
  /** Action invoked on Apply. None ⇒ value is persisted without server-side validation. */
  apply_action?: ActionRef | null;
}

export interface NumberInputControl {
  kind: 'number_input';
  id: string;
  label: string;
  tooltip?: string | null;
  default?: number | null;
  min?: number | null;
  max?: number | null;
  step?: number | null;
  on_change?: ActionRef | null;
}

export interface StatusDisplayControl {
  kind: 'status_display';
  id: string;
  label: string;
  tooltip?: string | null;
  source: ActionRef;
  /** `"{{field}}"` tokens resolve from the response JSON's top-level fields. */
  render_template: string;
}

export interface FilePickerControl {
  kind: 'file_picker';
  id: string;
  label: string;
  tooltip?: string | null;
  /** Allowed file extensions (without leading dot). Empty ⇒ any. Ignored when `directory: true`. */
  extensions?: string[];
  /** When true, the dialog selects a directory instead of a file. */
  directory?: boolean;
  on_change?: ActionRef | null;
}

export interface LinkControl {
  kind: 'link';
  id: string;
  label: string;
  tooltip?: string | null;
  href: string;
  /** `external` opens system browser, `internal` calls SvelteKit goto(). Default `external`. */
  target?: 'external' | 'internal' | string;
}

// ─── v0.2.32 new control kinds (L4, L5) ────────────────────────────────

/**
 * v0.2.32 L4 (2026-05-24): data source for InfoDynamicControl. The
 * `kind` tag is forward-compatible — v1 ships exactly one variant
 * (`module_db`); future kinds (`http_endpoint`, `tauri_command`) will
 * land additively without breaking older manifests.
 */
export type InfoDynamicSource = {
  kind: 'module_db';
  /** Module-DB table name (must be `{module_namespace}_*`). */
  table: string;
  /** Row key. May contain `{{project_id}}` (substituted by renderer). */
  key: string;
  /** Field to project from the row's JSON value. */
  field: string;
};

/**
 * v0.2.32 L4 (2026-05-24): live read-only info display bound to a
 * structured data source (currently `module_db`). The renderer reads
 * the source on mount and on manual refresh (`↻` button per section
 * that contains at least one `*_dynamic` control).
 *
 * `format` is a template with the single token `{value}` — replaced
 * with the source's resolved value (stringified for non-string
 * scalars). Default `"{value}"` (= just print the value verbatim).
 *
 * `fallback` renders when the source returns null (row absent, hub
 * unreachable, container never ran).
 */
export interface InfoDynamicControl {
  kind: 'info_dynamic';
  id: string;
  label: string;
  tooltip?: string | null;
  source: InfoDynamicSource;
  /** Default `"{value}"`. */
  format?: string;
  /** Rendered when source returns null. */
  fallback?: string | null;
}

/**
 * v0.2.32 L5 (2026-05-24): native HTML date picker.
 *
 * `default` accepts EITHER an ISO `YYYY-MM-DD` literal OR a keyword:
 *   - `"today"`  → today's date in the user's local wall clock
 *   - `"30_days_ago"` → today minus 30 days
 *   - `"90_days_ago"` → today minus 90 days
 *
 * Keyword resolution happens at mount time (renderer-side) so the
 * value matches the user's clock at the moment the control rendered.
 *
 * `min` / `max` are forwarded to the native `<input min/max>`
 * attributes verbatim — they must be ISO date literals (no keyword
 * resolution; manifests should declare concrete bounds).
 *
 * `on_change` fires after persistence (same flow as `text_input` /
 * `select`). The dispatcher exposes the new date via `{{value}}` and
 * the persisted value is available to sibling controls via
 * `{{control:<id>}}`.
 *
 * Clear affordance: a small "Clear" button next to the input writes
 * `null` to module_settings (represents "no date filter" — RL's
 * `/global/retrain` reads this as "all history").
 */
export interface DatePickerControl {
  kind: 'date_picker';
  id: string;
  label: string;
  tooltip?: string | null;
  /** ISO literal OR keyword `today` / `30_days_ago` / `90_days_ago`. */
  default?: string | null;
  /** ISO date lower-bound (`<input min>`). */
  min?: string | null;
  /** ISO date upper-bound (`<input max>`). */
  max?: string | null;
  on_change?: ActionRef | null;
}

/**
 * v0.2.33 (Agent D, 2026-05-25): forward-compat fallback for control
 * kinds this launcher version doesn't recognise. The Rust schema
 * deserialises unknown `kind` values as this variant in LENIENT mode
 * (the default). The renderer shows a placeholder card pointing the
 * user at "this requires a newer launcher version".
 *
 * `kind_string` carries the original unknown `kind` string from the
 * wire payload — useful for the placeholder's "Update to see <X>"
 * affordance. `raw` is the full unmodified JSON object so future
 * launcher versions could (e.g.) extract `id` / `label` for richer
 * placeholders without forcing a manifest version bump.
 */
export interface UnsupportedControl {
  kind: 'Unsupported';
  /** The original `kind` value the manifest declared (e.g. `"file_drop_zone"`). */
  kind_string: string;
  /** The raw JSON object that failed to deserialise. */
  raw: Record<string, unknown>;
}

export type ConfigControl =
  | CheckboxControl
  | MultiSelectControl
  | ButtonControl
  | SelectControl
  | InfoControl
  | TextInputControl
  | NumberInputControl
  | StatusDisplayControl
  | FilePickerControl
  | LinkControl
  // v0.2.32 additions:
  | InfoDynamicControl
  | DatePickerControl
  // v0.2.33 addition (forward-compat fallback):
  | UnsupportedControl;

// ─── ConfigSection + ConfigTab ──────────────────────────────────────────

export interface ConfigSection {
  title: string;
  description?: string | null;
  collapsible: boolean;
  initially_collapsed?: boolean;
  controls: ConfigControl[];
}

export interface ConfigTab {
  title: string;
  icon?: string | null;
  route?: string | null;
  description?: string | null;
  sections: ConfigSection[];
}

// ─── Helper type guards ─────────────────────────────────────────────────

/**
 * True when `action` is a structured ActionDescriptor (the v0.2.26
 * http path, the v0.2.32 chained_action path, or the v0.2.33
 * tauri_command path). The renderer dispatches all of them via the
 * single `module_dispatch_action` Tauri command — chaining +
 * tauri-command invocation logic lives in Rust.
 */
export function isActionDescriptor(action: ActionRef): action is ActionDescriptor {
  if (typeof action !== 'object' || action === null) return false;
  const kind = (action as { kind?: string }).kind;
  return kind === 'http' || kind === 'chained_action' || kind === 'tauri_command';
}

/** True when `action` is a legacy Tauri command name string. */
export function isLegacyAction(action: ActionRef): action is string {
  return typeof action === 'string';
}

/** True when `action` is the v0.2.32 chained_action variant. */
export function isChainedAction(
  action: ActionRef,
): action is ChainedActionDescriptor {
  return (
    typeof action === 'object' &&
    action !== null &&
    (action as { kind?: string }).kind === 'chained_action'
  );
}

/**
 * v0.2.33 (Agent D): True when `action` is the new tauri_command
 * variant — the launcher's dispatcher invokes a named Tauri command
 * with a strict whitelist gate.
 */
export function isTauriCommandAction(
  action: ActionRef,
): action is TauriCommandActionDescriptor {
  return (
    typeof action === 'object' &&
    action !== null &&
    (action as { kind?: string }).kind === 'tauri_command'
  );
}

/**
 * v0.2.33 (Agent D): True when `control` is the forward-compat
 * Unsupported placeholder (Rust schema's lenient-parse fallback for
 * unknown control kinds). The renderer maps this to a placeholder
 * card pointing the user at "update your launcher".
 */
export function isUnsupportedControl(
  control: ConfigControl,
): control is UnsupportedControl {
  return control.kind === 'Unsupported';
}
