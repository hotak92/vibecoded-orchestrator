// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.92 WP-12 — API wrapper + presentation logic for the model gateway
// card on the Services page.
//
// Everything the card DECIDES lives here rather than in the `.svelte` file,
// because this project's vitest setup is pure-node with no component
// renderer: logic left inside the component is logic no test can reach. What
// stays in the component is markup and event wiring.
//
// The decisions that matter — and why each is a function rather than an
// inline ternary:
//
//   * `describeStatus` collapses five inputs into one line WITHOUT losing
//     the tri-state. "Could not determine" has its own label; the whole
//     point of the Rust side reporting `reachable: null` is defeated if the
//     UI renders it as "stopped".
//   * `pointPanelWarnings` is the R15 surface: it is what tells the user
//     that a tier/subagent override already in their file is being carried
//     forward, and it must never be silent about one.
//   * `canStop` encodes the refusal that protects an unrelated process from
//     a signal aimed at a reused pid.

import { invoke } from '$lib/tauri';
import type {
  ModelGatewayStatus,
  StopOutcome,
  VSCodeInspection,
  VSCodeTarget,
  VSCodeTargets,
  VSCodeWriteResult,
} from '$lib/types/model-gateway';

/**
 * The GLM id offered as the pre-selected "Default model" when the user
 * chooses to set one at all.
 *
 * MUST MATCH `vco_lib/vscode_settings.py::DEFAULT_GATEWAY_MODEL`, pinned by
 * `tests/test_v0292_model_gateway_gui_contract.py`. GLM-5.3, never the flash
 * variant and never a pre-5.3 version.
 */
export const DEFAULT_GATEWAY_MODEL = 'claude-gw/glm-5.3';

/**
 * Env-block keys VCO never writes. Listed here only so the card can NAME
 * them when a user's file already contains one.
 *
 * MUST MATCH `vco_lib/vscode_settings.py::SLOT_OVERRIDE_KEYS`.
 */
export const SLOT_OVERRIDE_KEYS = [
  'ANTHROPIC_DEFAULT_OPUS_MODEL',
  'ANTHROPIC_DEFAULT_SONNET_MODEL',
  'ANTHROPIC_DEFAULT_HAIKU_MODEL',
  'ANTHROPIC_DEFAULT_FABLE_MODEL',
  'ANTHROPIC_SMALL_FAST_MODEL',
  'CLAUDE_CODE_SUBAGENT_MODEL',
] as const;

/** The project-module name gating the CLAUDE.md model-routing section. */
export const ROUTING_GUIDANCE_MODULE = 'model_gateway';

// ─── Commands ─────────────────────────────────────────────────────────────

export async function getModelGatewayStatus(): Promise<ModelGatewayStatus> {
  return invoke<ModelGatewayStatus>('model_gateway_status');
}

export async function startModelGateway(
  port?: number,
): Promise<ModelGatewayStatus> {
  return invoke<ModelGatewayStatus>('model_gateway_start', {
    port: port ?? null,
  });
}

export async function stopModelGateway(): Promise<StopOutcome> {
  return invoke<StopOutcome>('model_gateway_stop');
}

export async function setModelGatewayBoot(enabled: boolean): Promise<string> {
  return invoke<string>('model_gateway_set_boot', { enabled });
}

export async function checkModelGateway(): Promise<string> {
  return invoke<string>('model_gateway_check');
}

export async function listVSCodeTargets(): Promise<VSCodeTarget[]> {
  const res = await invoke<VSCodeTargets>('model_gateway_vscode_targets');
  return res.targets ?? [];
}

export async function inspectVSCodeTarget(
  path: string,
): Promise<VSCodeInspection> {
  return invoke<VSCodeInspection>('model_gateway_vscode_inspect', { path });
}

/**
 * Point the panel at the gateway.
 *
 * `model` is omitted unless the user explicitly picked one. That single
 * choice is R15 at the call site: VCO adds the catalogue, the user picks the
 * model, and nothing in between silently re-points a name.
 */
export async function pointPanelAtGateway(args: {
  path: string;
  model?: string | null;
  removeSlotOverrides?: boolean;
}): Promise<VSCodeWriteResult> {
  return invoke<VSCodeWriteResult>('model_gateway_point_panel', {
    path: args.path,
    model: args.model?.trim() ? args.model.trim() : null,
    removeSlotOverrides: args.removeSlotOverrides ?? false,
  });
}

export async function resetPanelToNative(
  path: string,
): Promise<VSCodeWriteResult> {
  return invoke<VSCodeWriteResult>('model_gateway_reset_native', { path });
}

/**
 * Turn the CLAUDE.md model-routing section on/off for one project.
 *
 * Reuses the generic module toggle rather than adding a command: that one
 * already writes the `project_modules` row AND re-renders the project's
 * CLAUDE.md, which is exactly the gate the template's
 * `{{#if_module_active model_gateway}}` block reads.
 */
export async function setProjectRoutingGuidance(
  projectId: string,
  enabled: boolean,
): Promise<void> {
  await invoke('set_project_module_enabled', {
    projectId,
    moduleName: ROUTING_GUIDANCE_MODULE,
    enabled,
  });
}

export async function projectHasRoutingGuidance(
  projectId: string,
): Promise<boolean> {
  return invoke<boolean>('is_project_module_active', {
    projectId,
    moduleName: ROUTING_GUIDANCE_MODULE,
  });
}

// ─── Presentation logic (pure — this is what the vitest covers) ───────────

export interface StatusLine {
  tone: 'up' | 'down' | 'warn' | 'unknown';
  label: string;
  detail: string;
}

/**
 * One line describing the gateway, preserving the tri-state.
 *
 * Order matters: "something is listening but is not our gateway" and "the
 * pid file is stale" are both states a naive up/down reading would report
 * wrongly, so they are checked before the simple cases.
 */
export function describeStatus(s: ModelGatewayStatus | null): StatusLine {
  if (!s) {
    return { tone: 'unknown', label: 'unknown', detail: 'Status not loaded yet.' };
  }
  if (s.reachable === null) {
    return {
      tone: 'unknown',
      label: 'unknown',
      detail:
        s.health_error ??
        `Could not determine whether anything is answering on ${s.base_url}.`,
    };
  }
  if (s.reachable && s.health) {
    // Family count comes from `catalog_source`, whose keys ARE the families
    // the gateway serves. `vendors` lists only the configured third-party
    // rows — claude is not one of them — so counting it and adding one would
    // be a guess that happens to be right today.
    const families = Object.keys(s.health.catalog_source).length;
    return {
      tone: 'up',
      label: 'running',
      detail: `v${s.health.version} on ${s.base_url}, serving ${families} model ${
        families === 1 ? 'family' : 'families'
      }.`,
    };
  }
  if (s.process === 'stale_pid_file') {
    return {
      tone: 'warn',
      label: 'stopped (stale pid file)',
      detail: `Nothing is answering on ${s.base_url}, and a pid file names a process (${s.pid}) that no longer exists. The next start replaces it.`,
    };
  }
  if (s.process === 'running') {
    return {
      tone: 'warn',
      label: 'starting or wedged',
      detail: `Process ${s.pid} is alive but ${s.base_url} did not answer: ${
        s.health_error ?? 'no reason given'
      }.`,
    };
  }
  return {
    tone: 'down',
    label: 'stopped',
    detail: s.token_present
      ? 'Not running. It has run before on this machine.'
      : 'Not running. It has never been started on this machine.',
  };
}

/** Stop is offered only when this launcher can prove which process to signal. */
export function canStop(s: ModelGatewayStatus | null): boolean {
  return !!s && s.supervised;
}

/**
 * Why the Stop button is disabled, in the user's terms. Empty when enabled.
 *
 * A disabled control with no explanation is a bug report waiting to happen,
 * and this one is disabled for a reason the user cannot guess.
 */
export function stopDisabledReason(s: ModelGatewayStatus | null): string {
  if (!s) return 'Status not loaded yet.';
  if (s.supervised) return '';
  if (s.process === 'running') {
    return `The gateway (pid ${s.pid}) was started outside this launcher, so this button will not signal it — a pid read from a file is not proof of which process it names. Turn "Start at login" off (that stops it on Linux and macOS; on Windows it removes the login task but leaves the running process), or stop it where you started it.`;
  }
  return 'Nothing to stop.';
}

/**
 * Warnings to show BEFORE the user points the panel at the gateway.
 *
 * The Remote Control note comes first and is never suppressed (it applies
 * to the JSONC paste path too): Remote Control is endpoint-gated — Claude
 * Code initializes it only in sessions talking directly to
 * api.anthropic.com, so it can never work in a panel pointed at the
 * gateway, whatever model is selected. The supported shape is a native
 * session alongside (see docs/TROUBLESHOOTING.md). Verified against
 * Claude Code 2.1.258; the gate shipped in v2.1.196.
 *
 * The slot-override warning is the R15 one and is never suppressed: keys
 * that re-point a model name are being carried forward, and the user has to
 * be told which, by name.
 */
export function pointPanelWarnings(inspection: VSCodeInspection | null): string[] {
  const out: string[] = [];
  if (!inspection) return out;
  out.push(
    'Remote Control (/remote-control, phone access from the Claude app) does not work in a panel pointed at the gateway: Claude Code enables it only in sessions talking directly to api.anthropic.com, and claude.ai sign-in does not change that. Keep a native terminal session for it — claude --remote-control — alongside the gateway panel (docs/TROUBLESHOOTING.md, "Remote Control").',
  );
  if (inspection.parseable === false) {
    out.push(
      `${inspection.path} is not strict JSON, so VCO will not rewrite it (that would delete your comments). Use the paste-ready block instead.`,
    );
    return out;
  }
  if (inspection.slot_overrides.length > 0) {
    out.push(
      `This settings file already sets ${inspection.slot_overrides.join(
        ', ',
      )}. VCO never writes those — they re-point a model name you selected at a different model. They will be carried forward unchanged unless you tick "also remove them".`,
    );
  }
  if (inspection.permissions === 'broader') {
    out.push(
      'This settings file is readable by other accounts on this machine, and it will hold the gateway token. VCO restricts it after writing, but VS Code can widen it again when you edit settings in its UI.',
    );
  }
  if (inspection.permissions === 'unknown' && inspection.exists) {
    out.push(
      'VCO could not determine who can read this settings file. Treat the gateway token in it as exposed until you have checked.',
    );
  }
  return out;
}

/** Summary of what a completed write did, for the toast. */
export function describeWriteResult(r: VSCodeWriteResult): string {
  if (!r.ok) return r.message;
  if (r.status === 'unchanged') return r.message;
  const bits: string[] = [];
  if (r.keys_written?.length) bits.push(`wrote ${r.keys_written.length} key(s)`);
  if (r.keys_preserved?.length) {
    bits.push(`kept ${r.keys_preserved.length} existing key(s)`);
  }
  if (r.keys_removed?.length) bits.push(`removed ${r.keys_removed.join(', ')}`);
  if (r.backup_path) bits.push('backup made');
  return bits.length ? `${r.message} (${bits.join('; ')})` : r.message;
}

/**
 * True when the gateway is configured enough for the panel actions and the
 * CLAUDE.md routing guidance to make sense.
 *
 * "Configured" is deliberately not "running": a user who has started it once
 * (so the token exists) and turned it off for the afternoon has a gateway.
 * A user who has never run it does not, and must not be offered routing
 * advice for models they cannot reach.
 */
export function gatewayIsConfigured(s: ModelGatewayStatus | null): boolean {
  if (!s) return false;
  return s.reachable === true || s.token_present || s.boot === 'enabled';
}
