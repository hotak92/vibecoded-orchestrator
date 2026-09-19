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
  /**
   * Seconds until the Claude login expires; negative once it has, `null`
   * when the credentials file states no expiry.
   *
   * It matters because NOTHING in the gateway refreshes that login: a panel
   * pointed at the gateway authenticates with the host token, so the native
   * refresh that a directly-connected panel performs never happens. A
   * gateway-only machine therefore goes dark when this reaches zero, and the
   * card warns before it does.
   */
  oauth_expires_in_s: number | null;
  vendors: string[];
  vendor_keys_cached: string[];
  /**
   * The scope the gateway's vendor keys resolve in, and whether it resolves
   * (v0.2.95, R5b).
   *
   * `vendors` beside an empty `vendor_keys_cached` reads like "no key
   * configured yet"; on 2026-09-10 the truth was "this daemon's working
   * directory is not a registered project, so it can see no key you
   * configure". `resolvable` is TRI-state — `null` means nothing has probed
   * it, which is a different claim from `false` and is rendered as one.
   */
  secret_scope?: GatewaySecretScope | null;
  /** Where per-chat token rows land, and how many this process has written. */
  usage_ledger?: GatewayUsageLedger | null;
  /** `owner_only` | `broader` | `unknown` for the gateway's token file. */
  token_file_permissions: string;
}

/** `/health`'s `secret_scope` block. */
export interface GatewaySecretScope {
  /** The project the gateway resolves secrets as. */
  project: string;
  /** `true` resolves, `false` does not, `null` nothing probed it yet. */
  resolvable: boolean | null;
  /** Why, in key names and paths only — never a value. */
  reason: string;
}

/** `/health`'s `usage_ledger` block. Counters and a path, never rows. */
export interface GatewayUsageLedger {
  /** `null` when the metrics home could not be resolved. */
  path: string | null;
  /** Rows THIS gateway process has appended. */
  rows_written: number;
  /** `ts` of the newest row, `null` before the first. */
  last_write_ts?: string | null;
}

/** One check inside a dogfood run. */
export interface DogfoodCase {
  case: string;
  ok: boolean;
  detail: string;
}

/**
 * `ok` — the gateway answered like Anthropic.
 * `refused` — it did NOT; `reason` is `dogfood:<case>`.
 * `skipped` — the comparison could not run (no Claude login, no network),
 *   which is deliberately not evidence against the gateway.
 */
export interface DogfoodVerdict {
  ok: boolean;
  status: 'ok' | 'refused' | 'skipped';
  reason: string | null;
  message: string;
  /** Absent on the CLI's own refusal envelopes (no proof ran). */
  cases?: DogfoodCase[];
  elapsed_s: number;
}

export type GatewayProcessState = 'running' | 'stale_pid_file' | 'not_running';
export type BootAutostart = 'enabled' | 'disabled' | 'unsupported';

/** Who would restart the gateway if it died. */
export type GatewaySupervision =
  | 'launcher'
  | 'boot_service'
  | 'unsupervised'
  | 'unknown'
  | 'not_running';

/**
 * The login registration's state — the toggle's THIRD position.
 *
 * `--boot-status` answers a binary question ("is a registration present?").
 * That is one state short of the truth, and the missing one is the state this
 * machine sat in for eight hours on 2026-09-10: registered, enabled, and
 * unable to run. Mirrors `vco_lib.gateway_ensure.GatewayState`.
 */
export type GatewayRegistrationState =
  | 'running'
  | 'registered_not_running'
  | 'registered_but_unrunnable'
  | 'not_registered'
  | 'start_failed'
  | 'disabled_by_env';

export interface GatewayRegistration {
  state: GatewayRegistrationState;
  /** The sentence to show. Every state is named, the silent ones included. */
  reason: string;
  /** `true` / `false` / `null` = not probed. Never collapsed to a boolean. */
  runnable: boolean | null;
  /** The unit / plist / task path, for a card that has to say WHERE. */
  unit_path: string | null;
}

/**
 * What the HUB's gateway supervisor concluded when it stopped retrying.
 *
 * The hub is detached and its stderr goes nowhere a user looks, so this row
 * is how "I tried three times and gave up" reaches the Services card. It is
 * deleted by the hub the moment the gateway serves again, so its presence is
 * always current.
 */
export interface HubGatewayCondition {
  state: string;
  reason: string;
  attempts: number;
  port: number;
  observed_at_ms: number;
}

export interface ModelGatewayStatus {
  process: GatewayProcessState;
  pid: number | null;
  /** True only when THIS launcher session started it (see the Rust docs). */
  supervised: boolean;
  /** See `supervision_word` in the Rust command module. */
  supervision: GatewaySupervision;
  /**
   * The dogfood verdict, present only on the payload a START returns.
   *
   * `vco_lib.vscode_settings.dogfood_gateway` sends one real request through
   * the gateway and the same one to api.anthropic.com and compares them, so
   * "started" means "answers like Anthropic" rather than "answered
   * /health". Absent on ordinary status polls — the proof costs seconds.
   */
  dogfood?: DogfoodVerdict | null;
  port: number;
  base_url: string;
  /** `true` reachable, `false` refused, `null` could not determine. */
  reachable: boolean | null;
  health: GatewayHealth | null;
  health_error: string | null;
  boot: BootAutostart;
  /**
   * The registration in full, including "registered but unrunnable".
   *
   * Absent means NOT ASKED — which is the case while the gateway answers,
   * because a serving gateway is proof its registration runs and the answer
   * costs a subprocess. It never means "not registered"; that is
   * `{ state: 'not_registered' }`.
   */
  registration?: GatewayRegistration | null;
  /** The hub supervisor's verdict, when it gave up. Absent = nothing wrong. */
  hub_condition?: HubGatewayCondition | null;
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
  /**
   * A Default write the writer DECLINED (a vendor id), naming the id and the
   * rule. Orthogonal to `ok`: the rest of the write still happened, which is
   * why it is its own field and not `reason`.
   */
  refusal_reason?: string | null;
  /** The ANTHROPIC_BASE_URL actually written, so a toast can name the port. */
  base_url?: string | null;
  /**
   * A vendor `ANTHROPIC_MODEL` already in the file and carried forward. Kept
   * (it is the user's key) and REPORTED, because a restart resumes on it.
   */
  vendor_default_preserved?: string | null;
  permissions: string;
  paste_block: string | null;
  restart_required: boolean;
}
