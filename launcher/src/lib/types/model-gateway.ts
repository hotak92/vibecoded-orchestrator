// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.92 WP-12 — wire types for the model gateway's GUI surface.
//
// Mirrors the serde shapes in
// `launcher/src-tauri/src/commands/model_gateway.rs` and the JSON emitted by
// `python -m vco_lib.vscode_settings`. Every "could not determine" is
// modelled as `null` rather than folded into a boolean: a status card that
// says "stopped" when it actually means "I could not reach it" is the exact
// defect the tri-state exists to prevent.

/** The gateway's `/health` payload. Fields default rather than error. */
export interface GatewayHealth {
  ok: boolean;
  service: string;
  version: string;
  port: number;
  host: string;
  /** vendor family -> `live` | `static` | `unfetched` | `unavailable`. */
  catalog_source: Record<string, string>;
  context_table_source: string;
  context_table_path: string | null;
  oauth_present: boolean;
  /** `present` | `expired` | `absent` | `unreadable`. */
  oauth_state: string;
  vendors: string[];
  vendor_keys_cached: string[];
  /** `owner_only` | `broader` | `unknown` for the gateway's token file. */
  token_file_permissions: string;
}

export type GatewayProcessState = 'running' | 'stale_pid_file' | 'not_running';
export type BootAutostart = 'enabled' | 'disabled' | 'unsupported';

export interface ModelGatewayStatus {
  process: GatewayProcessState;
  pid: number | null;
  /** True only when THIS launcher session started it (see the Rust docs). */
  supervised: boolean;
  port: number;
  base_url: string;
  /** `true` reachable, `false` refused, `null` could not determine. */
  reachable: boolean | null;
  health: GatewayHealth | null;
  health_error: string | null;
  boot: BootAutostart;
  token_present: boolean;
  python: string | null;
}

export interface StopOutcome {
  stopped: boolean;
  message: string;
}

/** One discovered VS Code-family `settings.json`. */
export interface VSCodeTarget {
  app_id: string;
  display_name: string;
  path: string;
  /** `native` | `flatpak` | `override`. */
  flavour: string;
}

export interface VSCodeTargets {
  targets: VSCodeTarget[];
}

/** Read-only description of one settings file. Nothing is guessed. */
export interface VSCodeInspection {
  path: string;
  exists: boolean;
  /** `null` when the file does not exist — not `false`. */
  parseable: boolean | null;
  refusal_reason: string | null;
  message: string | null;
  points_at_vco_gateway: boolean | null;
  base_url: string | null;
  model: string | null;
  discovery_enabled: boolean | null;
  disable_login_prompt: boolean | null;
  slot_overrides: string[];
  managed_keys_present: string[];
  /** `owner_only` | `broader` | `unknown`. */
  permissions: string;
}

/** Result of a `point`/`reset` action. A refusal is a normal outcome. */
export interface VSCodeWriteResult {
  action: string;
  path: string;
  ok: boolean;
  status: 'written' | 'unchanged' | 'refused' | 'left_alone';
  reason: string | null;
  message: string;
  backup_path: string | null;
  keys_written?: string[];
  keys_preserved?: string[];
  keys_removed?: string[];
  slot_overrides_preserved?: string[];
  /**
   * R41: env-block keys whose value gained the `[1m]` context-window hint
   * (same model, right window). The Python writer's `message` names them,
   * so the toast reports them; the field keeps the wire contract complete.
   */
  values_healed?: string[];
  permissions: string;
  paste_block: string | null;
  restart_required: boolean;
}
