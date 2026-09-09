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
 * The id offered as the pre-selected "Default model" when the user chooses
 * to set one at all. FIRST-PARTY, always.
 *
 * It was `claude-gw/glm-5.3` until 2026-09-08, and that pre-selection is how
 * a vendor id reached a real settings.json: ANTHROPIC_MODEL is what a
 * RESTARTED Claude Code panel resumes on, so the machine ran a whole release
 * cycle on GLM while every surface still said Fable. A vendor model belongs
 * in the picker, never in the Default.
 *
 * MUST MATCH `vco_lib/vscode_settings.py::DEFAULT_GATEWAY_MODEL`, pinned by
 * `tests/test_v0292_model_gateway_gui_contract.py`.
 */
export const DEFAULT_GATEWAY_MODEL = 'claude-opus-5';

/**
 * The gateway's vendor namespace and the client's context-window suffix.
 *
 * MUST MATCH `vco_lib/vscode_settings.py::GATEWAY_ID_PREFIX` /
 * `CONTEXT_1M_SUFFIX`; the parity test reads both files.
 */
export const GATEWAY_ID_PREFIX = 'claude-gw/';
export const CONTEXT_1M_SUFFIX = '[1m]';

/**
 * Prefix every Anthropic-served id carries, and the prefix the gateway routes
 * to api.anthropic.com. A PREFIX, case-folded first — review R1-5: a
 * substring test accepted `glm-5.3-claude` (a vendor id) and
 * `Claude-GW/glm-5.3` (the namespace in different case).
 *
 * MUST MATCH `vco_lib/vscode_settings.py::FIRST_PARTY_ID_PREFIX`.
 */
export const FIRST_PARTY_ID_PREFIX = 'claude-';

/**
 * The gateway's `/health` `service` value.
 *
 * MUST MATCH `vco_lib/vscode_settings.py::GATEWAY_SERVICE_NAME` and
 * `model_gateway.rs::GATEWAY_SERVICE`. Used to decide whether a start
 * actually produced OUR gateway before the GUI claims it did.
 */
export const GATEWAY_SERVICE_NAME = 'vct-model-gateway';

/**
 * Does this id name a model Anthropic serves under its own name?
 *
 * (C)-tier mirror of `vco_lib/vscode_settings.py::is_first_party_model_id`,
 * pinned by `tests/test_v0292_model_gateway_gui_contract.py`. It is a mirror
 * rather than a call because it runs on every keystroke in the Default-model
 * field, and spawning a Python process per character to answer a
 * three-substring question is not a trade this rule is worth. The Python
 * side refuses independently, so a drift here is a rejected write, never a
 * silent one.
 *
 * `claude-gw/claude-opus-5` is NOT first-party: the namespace means only the
 * gateway resolves it, so a panel that came back stock would fall back to a
 * name nothing answers.
 */
export function isFirstPartyModelId(modelId: string | null | undefined): boolean {
  if (typeof modelId !== 'string') return false;
  let base = modelId.trim().toLowerCase();
  if (!base) return false;
  if (base.endsWith(CONTEXT_1M_SUFFIX)) {
    base = base.slice(0, -CONTEXT_1M_SUFFIX.length).trim();
  }
  if (base.startsWith(GATEWAY_ID_PREFIX)) return false;
  return base.startsWith(FIRST_PARTY_ID_PREFIX);
}

/**
 * Why a Default-model entry is refused, in the user's terms. Empty when it
 * is acceptable — the inline-error contract the Services page renders.
 */
export function defaultModelError(modelId: string): string {
  if (!modelId.trim()) {
    return 'Enter a model id, or untick the box to leave the Default unset.';
  }
  if (isFirstPartyModelId(modelId)) return '';
  return `${modelId.trim()} is not a Claude model id, so VCO will not write it as the Default. The Default is what a restarted Claude Code panel falls back to — a vendor model there answers sessions you believe are on Claude. Pick it in the /model picker instead; the gateway puts it there.`;
}

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
  /** The port the caller KNOWS the gateway is on; null = let Rust resolve. */
  port?: number | null;
}): Promise<VSCodeWriteResult> {
  return invoke<VSCodeWriteResult>('model_gateway_point_panel', {
    path: args.path,
    model: args.model?.trim() ? args.model.trim() : null,
    removeSlotOverrides: args.removeSlotOverrides ?? false,
    port: args.port ?? null,
  });
}

/**
 * The port a panel write should carry, from a status card's own reading.
 *
 * The gateway's LIVE port when the status proves one (`gatewayIsLive`), null
 * otherwise — null keeps the pre-v0.2.94 behaviour of letting Rust resolve,
 * which is the honest answer when nothing is running to have a port. Same
 * class as R1-1: a write that re-resolves can name a different process than
 * the one the card is describing.
 */
export function pointPanelPort(status: ModelGatewayStatus | null): number | null {
  return gatewayIsLive(status) ? (status?.port ?? null) : null;
}

export async function resetPanelToNative(
  path: string,
): Promise<VSCodeWriteResult> {
  return invoke<VSCodeWriteResult>('model_gateway_reset_native', { path });
}

/**
 * Remove ANTHROPIC_MODEL and nothing else.
 *
 * The counterpart to the refusal: VCO will not WRITE a vendor Default, and a
 * file that already holds one is the user's — so it is surfaced with this
 * one-key action beside it rather than deleted behind their back.
 */
export async function clearPanelDefaultModel(
  path: string,
): Promise<VSCodeWriteResult> {
  return invoke<VSCodeWriteResult>('model_gateway_clear_default_model', { path });
}

// ─── The Multimodel <-> Remote Control switch ─────────────────────────────
//
// Remote Control is endpoint-gated in Claude Code (>= 2.1.196: refused
// whenever ANTHROPIC_BASE_URL is not api.anthropic.com, claude.ai login or
// not), and `claudeCode.environmentVariables` is VS Code machine-scope, so
// the user has exactly one of {gateway picker, Remote Control} at a time.
// The StatusBar's segmented control flips between them; the Python writer
// (`python -m vco_lib.vscode_settings mode`) owns every byte of the
// decision. What lives here is the wire shape and the copy.

/** The two states the switch writes, plus the two it only reports. */
export type PanelMode =
  | 'multimodel'
  | 'remote-control'
  | 'unmanaged'
  | 'unparseable';

/** The two states the switch can be asked to apply. */
export type SettablePanelMode = 'multimodel' | 'remote-control';

/**
 * MUST MATCH `vco_lib/vscode_settings.py::MODES` and
 * `model_gateway.rs::PANEL_MODES`. The Rust side refuses anything else
 * before spawning, and the CLI's argparse `choices` refuses it again.
 */
export const PANEL_MODES: readonly SettablePanelMode[] = [
  'multimodel',
  'remote-control',
] as const;

/** The gateway's liveness, as the Python writer probed it. Tri-state. */
export type GatewayProbeState = 'running' | 'stopped' | 'unreachable';

/** `mode --get`: read-only description of one settings file's state. */
export interface PanelModeReport {
  mode: PanelMode;
  path: string;
  /** One sentence for the tooltip / status line. */
  detail: string;
  base_url: string | null;
  model: string | null;
  slot_overrides: string[];
  /** A stash from a previous Remote Control switch is waiting. */
  stash_present: boolean;
  stash_path: string;
  /**
   * `/health` on the RESOLVED GATEWAY port: is there a VCO gateway on this
   * machine? `stopped` is the one state the frame offers to fix by starting
   * it; `unreachable` means something else may own that port and starting
   * blindly is how a collision happens.
   */
  gateway: GatewayProbeState;
  /**
   * The same probe against the port THIS PANEL talks to, or `null` when that
   * is not a loopback port (no endpoint, or a remote one).
   *
   * Review R2-3: a different question from `gateway`, and reading one as the
   * other produced a "Multimodel (gateway stopped)" label with a Start button
   * that then errored "already running" — the dead thing was the panel's
   * prototype endpoint, not the gateway.
   */
  endpoint: GatewayProbeState | null;
  /** `unmanaged`, but at a loopback port — a prototype gateway, most likely. */
  prototype_endpoint: boolean;
  /** ANTHROPIC_MODEL currently names a model VCO would refuse to write. */
  default_model_is_vendor: boolean;
}

/** `mode --set`: a write result plus what the switch stashed / restored. */
export interface PanelModeResult extends VSCodeWriteResult {
  mode: SettablePanelMode;
  /** Model/slot keys taken out of the file (remote-control leg). */
  values_stashed?: string[];
  /** Model/slot keys put back from the stash (multimodel leg). */
  keys_restored?: string[];
  stash_path?: string;
  stash_present?: boolean;
  /** Why a stash was NOT restored (made for another file, unreadable). */
  stash_skipped_reason?: string | null;
  /**
   * A Default write the writer declined (a vendor id), naming the id and the
   * rule. Orthogonal to `ok`: the rest of the write still happened.
   */
  refusal_reason?: string | null;
  /** A vendor Default already in the file, carried forward and reported. */
  vendor_default_preserved?: string | null;
}

export async function getPanelMode(path: string): Promise<PanelModeReport> {
  return invoke<PanelModeReport>('model_gateway_mode_get', { path });
}

/**
 * Apply a mode. The write is immediate; VS Code must be restarted by the
 * user to load it — the caller shows the persistent notice and never
 * automates the restart.
 */
export async function setPanelMode(
  path: string,
  mode: SettablePanelMode,
  port?: number | null,
): Promise<PanelModeResult> {
  // `port` is the port a just-started gateway actually bound. Without it the
  // Rust side re-resolves (launcher env -> the daemon's port file -> the
  // launcher's last-started-port record -> 11436) and can write
  // a base URL that names a DIFFERENT process — on the reporter's machine, a
  // legacy container on 11436, handed our token with a notice that said
  // "Applied" (review R1-1b).
  return invoke<PanelModeResult>('model_gateway_mode_set', {
    path,
    mode,
    port: port ?? null,
  });
}

export interface ModeDescription {
  /** Pill label. */
  label: string;
  /** One-sentence trade-off for the tooltip. */
  tooltip: string;
  /** Which pill (if any) renders filled. `null` = neither. */
  active: SettablePanelMode | null;
}

/**
 * Copy for each state. `unmanaged` and `unparseable` light no pill: the
 * first is the user's own endpoint (VCO leaves it alone), the second is a
 * file nothing should guess about.
 */
export function describeMode(mode: PanelMode | null): ModeDescription {
  switch (mode) {
    case 'multimodel':
      return {
        label: 'Multimodel',
        tooltip: 'GLM + Claude in one picker; Remote Control unavailable.',
        active: 'multimodel',
      };
    case 'remote-control':
      return {
        label: 'Remote Control',
        tooltip:
          'Stock Claude Code; phone Remote Control works; GLM models unavailable in the panel.',
        active: 'remote-control',
      };
    case 'unmanaged':
      return {
        label: 'Custom endpoint',
        tooltip: 'Panel points at a custom endpoint; VCO leaves it alone.',
        active: null,
      };
    case 'unparseable':
      return {
        label: 'Unreadable settings',
        tooltip:
          'settings.json is not strict JSON (comments or trailing commas); VCO will not rewrite it.',
        active: null,
      };
    default:
      return { label: 'Panel mode', tooltip: 'Panel mode not loaded yet.', active: null };
  }
}

/** Tooltip copy for each pill — the trade-off, one sentence each. */
export const MODE_PILL_TOOLTIP: Record<SettablePanelMode, string> = {
  multimodel: describeMode('multimodel').tooltip,
  'remote-control': describeMode('remote-control').tooltip,
};

/** The port of a base URL, as a string, or `null` when there is none. */
export function endpointPort(baseUrl: string | null | undefined): string | null {
  if (!baseUrl) return null;
  try {
    const port = new URL(baseUrl).port;
    return port || null;
  } catch {
    return null;
  }
}

/**
 * `describeMode`, plus the one `unmanaged` case that has a migration.
 *
 * A loopback endpoint that is not the VCO gateway is almost always a
 * prototype gateway (the 2026-09-08 machine had one on 8787 while the
 * product gateway had never been started). Saying "VCO leaves it alone"
 * there is true and useless: clicking Multimodel IS the migration, so the
 * pill stays enabled and the copy says what it does.
 */
export function describeModeReport(report: PanelModeReport | null): ModeDescription {
  if (!report) return describeMode(null);
  if (report.mode === 'unmanaged' && report.prototype_endpoint) {
    const port = endpointPort(report.base_url);
    return {
      label: 'Custom local endpoint',
      tooltip: `Panel points at a custom local endpoint on port ${
        port ?? '?'
      } (a prototype gateway?). Multimodel moves it to the VCO gateway.`,
      active: null,
    };
  }
  return describeMode(report.mode);
}

/** Pill label — names a stopped gateway instead of failing on the click. */
export function multimodelPillLabel(report: PanelModeReport | null): string {
  return report?.gateway === 'stopped' ? 'Multimodel (gateway stopped)' : 'Multimodel';
}

/**
 * Is a Start needed before pointing the panel?
 *
 * Anything but a confirmed `running` gateway. `unreachable` included: the
 * port may be owned by something else entirely, and the Rust starter's
 * collision fallback picks a free one — which is exactly the state that
 * needs a start, not a refusal.
 */
export function gatewayStartNeeded(report: PanelModeReport | null): boolean {
  return report?.gateway !== 'running';
}

/**
 * The panel's endpoint is not answering — a warning, never a Start button.
 *
 * Empty when there is nothing to say: no local endpoint (`null`), or one that
 * answers. Starting the VCO gateway does not fix a dead PROTOTYPE endpoint,
 * so this text points at the switch instead (review R2-3).
 */
export function endpointWarning(report: PanelModeReport | null): string {
  if (!report || report.endpoint === null || report.endpoint === 'running') return '';
  // Review R3-5: in `multimodel` the panel ALREADY points at the VCO gateway,
  // so "Multimodel points it at the VCO gateway" is advice for a click that
  // early-returns (the pill is the current mode). The stopped-gateway label
  // and the Start action beside it are the live remedy; this line would only
  // send the user at an inert control.
  if (report.mode === 'multimodel') return '';
  const port = endpointPort(report.base_url);
  const where = port ? `on port ${port}` : 'it points at';
  return report.endpoint === 'stopped'
    ? `Nothing is answering ${where} — the endpoint this panel talks to is down. Multimodel points it at the VCO gateway.`
    : `The endpoint this panel talks to ${where} did not answer as a VCO gateway. Multimodel points it at the VCO gateway.`;
}

/** The one-line warning shown when the file holds a vendor Default. */
export const VENDOR_DEFAULT_WARNING =
  'Default model is a vendor model: a Claude Code restart falls back to it, so sessions you believe are on Claude answer on it.';

export function vendorDefaultWarning(report: PanelModeReport | null): string {
  if (!report?.default_model_is_vendor) return '';
  return report.model
    ? `${VENDOR_DEFAULT_WARNING} (${report.model})`
    : VENDOR_DEFAULT_WARNING;
}

/**
 * Why a pill is disabled, in the user's terms. Empty when clickable.
 *
 * Both need a settings file to write. An unparseable file is refused by the
 * writer anyway; saying so here saves the click.
 *
 * What is deliberately NOT a reason any more: "the gateway has never run".
 * Until 2026-09-08 that disabled the Multimodel pill on exactly the machines
 * that needed it most — a fresh install, or one still on a prototype gateway
 * — with a sentence ("start it once, Services page") the user then had to
 * act on somewhere else. The pill now STARTS the gateway and then points, so
 * the only honest refusal left is a launcher that cannot start one at all
 * (no interpreter resolved).
 */
export function modeSwitchDisabledReason(
  target: SettablePanelMode,
  status: ModelGatewayStatus | null,
  targets: VSCodeTarget[],
  current: PanelModeReport | null,
): string {
  if (targets.length === 0) {
    return 'No VS Code-family settings.json was found on this machine.';
  }
  if (current?.mode === 'unparseable') {
    return describeMode('unparseable').tooltip;
  }
  if (target === 'multimodel' && status && status.python === null) {
    return 'No Python interpreter was found to run the model gateway with. Re-run install.py to rebuild the orchestrator venv.';
  }
  return '';
}

// ─── Clicking Multimodel: start the gateway first when it is not running ──

/** Refusal code the Python writer returns when the token file is missing. */
export const NO_HOST_TOKEN_REASON = 'no_host_token';

/** How long to wait before the ONE retry after starting a gateway. */
export const TOKEN_RACE_RETRY_DELAY_MS = 1000;

export function isMissingTokenRefusal(r: PanelModeResult): boolean {
  return !r.ok && r.reason === NO_HOST_TOKEN_REASON;
}

export interface MultimodelSwitchDeps {
  startGateway: () => Promise<ModelGatewayStatus>;
  /**
   * `port` is not optional decoration: this function's whole job after a
   * start is to hand the port the gateway actually bound to the panel write
   * (R1-1b). Typing it away made the two call sites below a TS2554 waiting
   * for a stricter checker — review R2-1.
   */
  setMode: (
    path: string,
    mode: SettablePanelMode,
    port?: number | null,
  ) => Promise<PanelModeResult>;
  sleep: (ms: number) => Promise<void>;
}

export interface MultimodelSwitchOutcome {
  result: PanelModeResult | null;
  /** A gateway was started as part of this click. */
  started: boolean;
  /**
   * Port the started gateway bound (it may differ from the default — the
   * Rust starter moves off an occupied port). Passed to the panel write so
   * the base URL names the process we just started, not a re-resolution.
   */
  startedPort: number | null;
  /**
   * The started gateway ANSWERED `/health` with our service name. Only then
   * may the GUI say it started (review R1-7): `status.port` alone is the
   * port we asked for, not evidence anything is listening on it.
   */
  startedLive: boolean;
  /** The start failed; `result` is null and this is the whole story. */
  startError: string | null;
  /** The one token-race retry fired. */
  retried: boolean;
}

/** Proof — not assumption — that a status describes a live VCO gateway. */
export function gatewayIsLive(status: ModelGatewayStatus | null): boolean {
  return !!status && status.reachable === true && status.health?.service === GATEWAY_SERVICE_NAME;
}

/**
 * The Multimodel click, in one testable function.
 *
 * Before 2026-09-08 this was a bare `setPanelMode`, and on a machine whose
 * gateway had never run it failed with "the gateway's host token file could
 * not be read … Start the model gateway once" — an error whose remedy the
 * click itself could have performed. So: start it when it is not running,
 * then point. The gateway writes its token file as it boots, so a point
 * issued microseconds later can still lose that race; that is what the ONE
 * retry is for, and after it the writer's real reason is shown rather than
 * retried forever.
 */
export async function switchToMultimodel(
  path: string,
  report: PanelModeReport | null,
  deps: Partial<MultimodelSwitchDeps> = {},
): Promise<MultimodelSwitchOutcome> {
  const startGateway = deps.startGateway ?? (() => startModelGateway());
  const setMode = deps.setMode ?? setPanelMode;
  const sleep =
    deps.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));

  const outcome: MultimodelSwitchOutcome = {
    result: null,
    started: false,
    startedPort: null,
    startedLive: false,
    startError: null,
    retried: false,
  };

  if (gatewayStartNeeded(report)) {
    try {
      const status = await startGateway();
      outcome.started = true;
      outcome.startedPort = status?.port ?? null;
      outcome.startedLive = gatewayIsLive(status);
    } catch (e) {
      // Not fatal on its own: a gateway started outside this launcher (or
      // one this launcher already supervises) refuses a second start, and
      // the point below may still succeed. The reason is carried either way.
      outcome.startError = String(e);
    }
  }

  outcome.result = await setMode(path, 'multimodel', outcome.startedPort);
  if (outcome.started && isMissingTokenRefusal(outcome.result)) {
    await sleep(TOKEN_RACE_RETRY_DELAY_MS);
    outcome.result = await setMode(path, 'multimodel', outcome.startedPort);
    outcome.retried = true;
  }
  return outcome;
}

/** The notice line for a Multimodel click: what happened, in order. */
export function describeSwitchOutcome(o: MultimodelSwitchOutcome): {
  tone: 'ok' | 'err';
  text: string;
} {
  const bits: string[] = [];
  if (o.started) {
    // Claim a start ONLY on proof (review R1-7). "Started on port N" from a
    // port number alone is the same class of guess as the fixed sleep the
    // Rust side used to do before it polled /health.
    if (o.startedLive && o.startedPort) {
      bits.push(`Started the model gateway on port ${o.startedPort}.`);
    } else if (o.startedLive) {
      bits.push('Started the model gateway.');
    } else {
      bits.push(
        o.startedPort
          ? `Asked the model gateway to start on port ${o.startedPort}; it is not answering yet.`
          : 'Asked the model gateway to start; it is not answering yet.',
      );
    }
  }
  if (!o.result) {
    return {
      tone: 'err',
      text: [...bits, o.startError ?? 'The model gateway could not be started.'].join(' '),
    };
  }
  if (!o.result.ok && o.startError) bits.push(o.startError);
  bits.push(describeModeResult(o.result));
  const tone = o.result.ok && (!o.started || o.startedLive) ? 'ok' : 'err';
  return { tone, text: bits.join(' ') };
}

/** The persistent notice after a successful switch. Never automated. */
export const RESTART_NOTICE = 'Applied — restart VS Code to load it';

/**
 * One line for the notice area after a switch: the restart reminder on a
 * write, the writer's own message on a refusal or a no-op.
 */
export function describeModeResult(r: PanelModeResult): string {
  if (!r.ok) return r.message;
  if (r.status === 'unchanged') return r.message;
  const bits: string[] = [];
  if (r.values_stashed?.length) bits.push(`stashed ${r.values_stashed.join(', ')}`);
  if (r.keys_restored?.length) bits.push(`restored ${r.keys_restored.join(', ')}`);
  if (r.values_healed?.length) bits.push(`[1m] added to ${r.values_healed.join(', ')}`);
  let line = bits.length ? `${RESTART_NOTICE} (${bits.join('; ')})` : RESTART_NOTICE;
  // A declined Default must never be the one thing the notice omits — that
  // silence is how the panel came back on a vendor model unnoticed.
  if (r.refusal_reason) line = `${line}. ${r.refusal_reason}`;
  // Same reasoning for one the writer KEPT (review R1-3): the migration click
  // leaves a vendor Default in place on purpose, and a notice that says only
  // "Applied" hands back a panel that still resumes on it.
  if (r.vendor_default_preserved) {
    line = `${line} Kept your Default ${r.vendor_default_preserved} — a vendor model; use “Clear default” to remove it.`;
  }
  return line;
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
  let line = bits.length ? `${r.message} (${bits.join('; ')})` : r.message;
  // Name the endpoint that was WRITTEN, not the one the card assumed. On a
  // machine where the default port belongs to something else, "pointed at
  // 11437" is the difference between a correct write and a silent one.
  if (r.base_url) line = `${line} Pointed at ${r.base_url}.`;
  // NOT a kept-Default sentence here (review R3-4): `r.message` is the Python
  // writer's own, and it already names a preserved vendor Default. Appending
  // a second one printed it twice in the Services toast. `describeModeResult`
  // does append it, because the mode switch replaces the writer's message
  // with the restart notice instead of carrying it.
  return line;
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

/**
 * Who would restart this gateway if it died — said plainly, including when
 * the answer is "nobody".
 *
 * A running gateway nobody supervises is the state that produced a silent
 * outage: the card said "running", the process died an hour later, and the
 * next thing the user saw was their editor failing. "Hand-started" is the
 * word for it because it is also the remedy's clue — Start at login is the
 * toggle right beside this line.
 *
 * `unknown` is kept distinct from `unsupervised` on purpose: the Rust side
 * only claims a supervisor it could verify, and a card that upgrades "I
 * could not ask" to "supervised" would re-introduce the same false comfort.
 */
export function describeSupervision(s: ModelGatewayStatus | null): StatusLine | null {
  if (!s || s.supervision === 'not_running') return null;
  switch (s.supervision) {
    case 'launcher':
      return {
        tone: 'up',
        label: 'supervised by this launcher',
        detail:
          'This launcher started it and will restart it once if it dies. Closing the launcher leaves it running but unsupervised.',
      };
    case 'boot_service':
      return {
        tone: 'up',
        label: 'supervised at login',
        detail:
          'The login service owns this process and restarts it on failure.',
      };
    case 'unsupervised':
      return {
        tone: 'warn',
        label: 'unsupervised (hand-started)',
        detail:
          'Nothing will restart this gateway if it dies — your editor would simply start failing. Turn on Start at login for a supervised one.',
      };
    default:
      return {
        tone: 'unknown',
        label: 'supervision unknown',
        detail:
          'Start at login is on, but this machine could not be asked whether the login service owns this exact process.',
      };
  }
}

/**
 * The dogfood verdict, when it is worth showing.
 *
 * A refusal is the loudest thing this card can say: the gateway answered
 * DIFFERENTLY from Anthropic on a real request, so pointing a panel at it
 * would hand the user a session that fails in a way they cannot diagnose.
 * `skipped` is silent — a machine with no Claude login or no network has a
 * perfectly good gateway and nothing to compare it against.
 */
export function describeDogfood(s: ModelGatewayStatus | null): StatusLine | null {
  const verdict = s?.dogfood;
  if (!verdict || verdict.status !== 'refused') return null;
  // `?? []`: the verdict may come from the CLI's own refusal envelope (a
  // missing host token), which has no cases — and this runs inside a
  // `$derived`, where a throw takes the whole card down.
  const failed = (verdict.cases ?? []).find((c) => !c.ok);
  return {
    tone: 'down',
    label: `gateway answered differently from Anthropic (${verdict.reason ?? 'unknown check'})`,
    detail: `${verdict.message}${failed ? ` [${failed.case}: ${failed.detail}]` : ''}`,
  };
}

/** Warn this long before the Claude login expires. */
export const OAUTH_WARN_SECONDS = 30 * 60;

/**
 * The Claude login's remaining life, when it is short enough to act on.
 *
 * Nothing in the gateway refreshes that login: a panel pointed at the
 * gateway presents the host token, so the refresh a directly-connected panel
 * performs never happens, and a gateway-only machine goes dark when the
 * token expires. `null` here means "nothing to say" — plenty of time left,
 * no expiry stated, or no login at all (which the status line already
 * covers).
 */
export function describeOAuthExpiry(s: ModelGatewayStatus | null): StatusLine | null {
  const seconds = s?.health?.oauth_expires_in_s;
  if (seconds === null || seconds === undefined) return null;
  if (seconds <= 0) {
    return {
      tone: 'down',
      label: 'Claude login expired',
      detail:
        'The gateway cannot serve first-party models until you run `claude` (or open a native panel) once to refresh the login.',
    };
  }
  if (seconds > OAUTH_WARN_SECONDS) return null;
  const minutes = Math.max(1, Math.round(seconds / 60));
  return {
    tone: 'warn',
    label: `Claude login expires in ${minutes} min`,
    detail:
      'Run `claude` (or open a native panel) once before then: the gateway reads that login and never refreshes it itself.',
  };
}
