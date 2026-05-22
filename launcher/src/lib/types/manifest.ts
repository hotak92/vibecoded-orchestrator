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

/** Declarative action that the generic `module_dispatch_action` Tauri command executes. */
export type ActionDescriptor = {
  kind: 'http';
  method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  path: string;
  body?: unknown;
  polling?: PollingSpec | null;
  /** Chain a follow-up action on success. Bounded by `max_chain_steps` (default 1024). */
  next_action?: ActionDescriptor | null;
};

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

export interface MultiSelectControl {
  kind: 'multi_select';
  id: string;
  label: string;
  tooltip?: string | null;
  options_source: ActionRef;
  on_change?: ActionRef | null;
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
  | LinkControl;

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

/** True when `action` is a structured ActionDescriptor (the v0.2.26 path). */
export function isActionDescriptor(action: ActionRef): action is ActionDescriptor {
  return typeof action === 'object' && action !== null && (action as ActionDescriptor).kind === 'http';
}

/** True when `action` is a legacy Tauri command name string. */
export function isLegacyAction(action: ActionRef): action is string {
  return typeof action === 'string';
}
