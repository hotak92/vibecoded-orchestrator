// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Where VCO's core services (Weaviate, Ollama, code-embed) run — the
// frontend half of the v0.2.97 service-endpoints design.
//
// The truth is one launcher.db table, `service_endpoints`: one row per
// service, in one of three modes —
//   * `vco_managed`       VCO's compose owns the container;
//   * `adopted_container` someone else's container, started/stopped BY NAME
//                         only, never recreated;
//   * `adopted_external`  a URL (native process, remote host); no lifecycle.
// Rows are written ONLY by `python -m vco_lib.service_endpoints`; the
// launcher reads them (`services_status`) and changes them through that
// module's verbs (`services_endpoint_action`), never by writing the DB.
//
// Every decision the Services page and the adoption dialog make lives here
// as a pure function so vitest can pin it (the pages are markup + wiring).
// There is deliberately NO "refuse" and NO "reset adoption" any more: a
// service always has an endpoint, and changing it is an explicit choice of
// another one.

import { invoke } from '$lib/tauri';

export type CoreServiceName = 'weaviate' | 'ollama' | 'code_embed';
export type EndpointMode = 'vco_managed' | 'adopted_container' | 'adopted_external';

/** A `service_endpoints` row as the launcher serializes it. */
export interface ServiceEndpointRow {
  service: string;
  mode: EndpointMode;
  scheme: string;
  host: string;
  port: number;
  grpc_port: number | null;
  container_name: string | null;
  compose_project: string | null;
  /** Raw `{"kind":"bind|volume","source":…,"destination":…}`. */
  data_mount_json: string | null;
  enabled: boolean;
  autostart: boolean;
  source: string;
  confirmed_by_user: boolean;
  verified_at: number | null;
  updated_at: number;
}

/** One service in the `services_status` snapshot (Rust `ServiceRuntimeState`). */
export interface ServiceRuntimeState {
  name: string;
  running: boolean;
  port: number;
  url: string;
  externally_managed: boolean;
  /** `null` only for a non-endpoint row (the hub's model_gateway). */
  mode: EndpointMode | null;
  /** The row; `null` when none is recorded yet (compiled default in use). */
  endpoint: ServiceEndpointRow | null;
  container_name: string | null;
  zombie?: boolean;
  /** Where this service runs waits for the user's choice (Rust `awaits_choice`). */
  pending_choice?: boolean;
}

export interface ServicesRuntimeSnapshot {
  services: ServiceRuntimeState[];
  runtime: string | null;
  needs_podman_machine_start: boolean;
  /** A core service has no row yet (install/update not finished). */
  endpoints_missing: boolean;
  degraded?: boolean;
}

export interface DataMount {
  kind: 'bind' | 'volume';
  source: string;
  destination: string;
}

/** A container the Python detector inspected (`ContainerInfo.to_json`). */
export interface DetectedContainer {
  name: string;
  image?: string | null;
  state?: string | null;
  compose_project?: string | null;
  host_ports?: Record<string, number>;
  mounts?: DataMount[];
}

/**
 * One candidate exactly as `python -m vco_lib.service_endpoints candidates
 * --json` reports it (`vco_lib/service_detection.py::Candidate.to_json`).
 */
export interface DetectedCandidate {
  service: string;
  url: string;
  host: string;
  port: number;
  grpc_port?: number | null;
  live?: boolean;
  has_vco_data?: boolean;
  vco_markers?: string[];
  version?: string | null;
  compatible?: boolean;
  reason?: string | null;
  ownership?: string | null;
  origins?: string[];
  container?: DetectedContainer | null;
}

/** The detector's whole reply (`{"schema": 1, "candidates": {…}}`), or the
 *  launcher's `{"error": …}` when detection could not run. */
export interface DetectionReport {
  schema?: number;
  candidates?: Partial<Record<CoreServiceName, DetectedCandidate[]>>;
  error?: string;
}

/** The boot event (`vct-external-services-detected`) payload. */
export interface ChoiceEvent {
  pending: CoreServiceName[];
  rows: Partial<Record<CoreServiceName, ServiceEndpointRow | null>>;
  detection: DetectionReport;
}

/**
 * One candidate as the dialog shows it — built from a {@link DetectedCandidate}
 * by {@link buildCandidateReport}.
 */
export interface EndpointCandidate {
  kind?: 'container' | 'process' | 'remote' | string;
  container_name?: string | null;
  compose_project?: string | null;
  image?: string | null;
  state?: string | null;
  url: string;
  port?: number | null;
  grpc_port?: number | null;
  /** VCO data found at this endpoint (KG collections / pulled models). */
  vco_data?: { found: boolean; summary?: string | null } | null;
  /** The compatibility gate; `false` ⇒ not adoptable, `incompatible_reason` says why. */
  compatible?: boolean;
  incompatible_reason?: string | null;
  data_mount?: DataMount | null;
  /** The detector's pick for this service. */
  recommended?: boolean;
  /** This candidate is what the service's row already points at. */
  current?: boolean;
}

export interface ServiceCandidates {
  row: ServiceEndpointRow | null;
  /** The user must choose (owner ruling: a third-party Weaviate is never adopted silently). */
  pending_consent?: boolean;
  /** Why the choice is pending, in the detector's words. */
  reason?: string | null;
  candidates: EndpointCandidate[];
}

export interface CandidateReport {
  ok: boolean;
  services: Partial<Record<CoreServiceName, ServiceCandidates>>;
  /** Detection could not run (the dialog can still offer VCO's own copy). */
  error?: string | null;
}

/** The Rust `EndpointAction` (serde tag `action`). */
export type EndpointAction =
  | { action: 'adopt'; service: CoreServiceName; container?: string; url?: string; accept_empty_kg?: boolean }
  | { action: 'use_vco_copy'; service: CoreServiceName; port?: number; accept_empty_kg?: boolean }
  | { action: 'hand_to_vco'; service: CoreServiceName };

export const CORE_SERVICES: readonly CoreServiceName[] = ['weaviate', 'ollama', 'code_embed'];

export const SERVICE_LABELS: Record<CoreServiceName, string> = {
  weaviate: 'Weaviate',
  ollama: 'Ollama',
  code_embed: 'Code embedding',
};

export function isCoreService(name: string): name is CoreServiceName {
  return (CORE_SERVICES as readonly string[]).includes(name);
}

export function serviceLabel(name: string): string {
  return isCoreService(name) ? SERVICE_LABELS[name] : name;
}

/** `scheme://host[:port]` — the same render as the Rust/Python mirror. */
export function rowUrl(row: ServiceEndpointRow): string {
  const defaultPort = row.scheme === 'https' ? 443 : row.scheme === 'http' ? 80 : null;
  return defaultPort === row.port ? `${row.scheme}://${row.host}` : `${row.scheme}://${row.host}:${row.port}`;
}

/** The row's data mount, or `null` when none is recorded / it is unreadable. */
export function parseDataMount(raw: string | null | undefined): DataMount | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as Partial<DataMount>;
    if ((v.kind === 'bind' || v.kind === 'volume') && typeof v.source === 'string' && v.source) {
      return { kind: v.kind, source: v.source, destination: typeof v.destination === 'string' ? v.destination : '' };
    }
  } catch {
    // An unreadable mount is shown as unknown, never guessed.
  }
  return null;
}

/** One line for a data mount: `volume vco_weaviate_data → /var/lib/weaviate`. */
export function describeDataMount(m: DataMount | null): string {
  if (!m) return 'not recorded';
  const kind = m.kind === 'bind' ? 'folder' : 'volume';
  return m.destination ? `${kind} ${m.source} → ${m.destination}` : `${kind} ${m.source}`;
}

export type BadgeTone = 'managed' | 'adopted' | 'external' | 'pending';

/** The mode badge on a Services row. */
export function modeBadge(s: ServiceRuntimeState): { label: string; tone: BadgeTone; title: string } {
  const row = s.endpoint;
  if (!row) {
    return {
      label: 'Not recorded yet',
      tone: 'pending',
      title:
        'No endpoint is recorded for this service yet. A finished install or update records it; ' +
        'use Detect services to choose now.',
    };
  }
  switch (row.mode) {
    case 'vco_managed':
      if (s.pending_choice) {
        return {
          label: 'Waiting for your choice',
          tone: 'pending',
          title: 'VCO found an instance it did not start and waits for you to choose: use it, or run VCO’s own copy.',
        };
      }
      return {
        label: row.enabled ? 'VCO-managed' : 'VCO-managed (disabled)',
        tone: 'managed',
        title: 'VCO runs this container from its own compose file.',
      };
    case 'adopted_container':
      return {
        label: `Using your container ${row.container_name ?? ''}`.trim(),
        tone: 'adopted',
        title: 'VCO starts and stops this container by name; it never removes or recreates it.',
      };
    case 'adopted_external':
      return {
        label: `Using ${rowUrl(row)}`,
        tone: 'external',
        title: 'VCO only connects to this endpoint; starting and stopping it is up to you.',
      };
  }
}

export type ServiceActionId = 'start' | 'stop' | 'restart' | 'recover' | 'change' | 'hand_to_vco';

/**
 * The buttons a Services row offers. Never "refuse" or "reset": changing
 * where a service runs is "Change…" (pick another endpoint), and the
 * opt-in ownership transfer is "Let VCO manage it" (Weaviate/Ollama
 * containers only — owner ruling Q2).
 */
export function serviceActions(s: ServiceRuntimeState): ServiceActionId[] {
  const out: ServiceActionId[] = [];
  const mode: EndpointMode = s.endpoint?.mode ?? s.mode ?? 'vco_managed';
  const adoptable = s.name === 'weaviate' || s.name === 'ollama';
  // Waiting for the user's choice (the disabled Weaviate row, owner ruling
  // Q1): nothing runs yet, so the one thing to do is choose — use that
  // instance (`adopt`) or run VCO's own copy (`use-vco-copy`).
  if (s.pending_choice && adoptable) return ['change'];
  if (mode === 'vco_managed' || mode === 'adopted_container') {
    out.push('start', 'stop', 'restart');
    if (s.zombie) out.push('recover');
  }
  if (adoptable) out.push('change');
  if (mode === 'adopted_container' && adoptable) out.push('hand_to_vco');
  return out;
}

export const ACTION_LABELS: Record<ServiceActionId, string> = {
  start: 'Start',
  stop: 'Stop',
  restart: 'Restart',
  recover: 'Recover',
  change: 'Change…',
  hand_to_vco: 'Let VCO manage it',
};

/** The services a candidate report asks the user about. */
export function pendingServices(report: CandidateReport | null): CoreServiceName[] {
  if (!report) return [];
  return CORE_SERVICES.filter((s) => report.services[s]?.pending_consent === true);
}

/** Can this candidate be adopted? (The detector's compatibility gate.) */
export function isAdoptable(c: EndpointCandidate): boolean {
  return c.compatible !== false;
}

/**
 * Leaving the Weaviate VCO uses now loses its KG from VCO's view (plan
 * invariant I6) — true when the endpoint the row points at holds VCO data,
 * which then needs the explicit confirmation (`accept_empty_kg`). Unknown ⇒
 * ask: a confirmation too many is cheap. A row that is only the
 * "confirmation pending" placeholder (VCO-managed, disabled) holds nothing.
 */
export function leavingVcoData(service: CoreServiceName, sc: ServiceCandidates | null | undefined): boolean {
  if (service !== 'weaviate' || !sc?.row) return false;
  if (sc.row.mode === 'vco_managed' && !sc.row.enabled) return false;
  const current = sc.candidates.find((c) => c.current === true);
  if (!current) return true;
  return current.vco_data?.found !== false;
}

/** "Run VCO's own copy" needs the KG confirmation. */
export function vcoCopyNeedsKgConfirm(service: CoreServiceName, sc: ServiceCandidates | null | undefined): boolean {
  return leavingVcoData(service, sc);
}

/** "Use this one" on `c` needs the KG confirmation: leaving VCO data for an
 *  endpoint that holds none (the verb refuses without `accept_empty_kg`). */
export function adoptNeedsKgConfirm(
  service: CoreServiceName,
  sc: ServiceCandidates | null | undefined,
  c: EndpointCandidate,
): boolean {
  return !c.current && c.vco_data?.found !== true && leavingVcoData(service, sc);
}

/** The action "Use this one" sends for `c`: its container by name, else its URL. */
export function adoptActionFor(service: CoreServiceName, c: EndpointCandidate, acceptEmptyKg = false): EndpointAction {
  const base: EndpointAction = c.container_name
    ? { action: 'adopt', service, container: c.container_name }
    : { action: 'adopt', service, url: c.url };
  return acceptEmptyKg ? { ...base, accept_empty_kg: true } : base;
}

/** The action "Run VCO's own copy" sends. */
export function vcoCopyAction(service: CoreServiceName, acceptEmptyKg: boolean): EndpointAction {
  return acceptEmptyKg
    ? { action: 'use_vco_copy', service, accept_empty_kg: true }
    : { action: 'use_vco_copy', service };
}

/** Candidate facts worth a line in the dialog, in display order. */
export function candidateFacts(c: EndpointCandidate): string[] {
  const facts: string[] = [];
  if (c.container_name) facts.push(`container ${c.container_name}`);
  else if (c.kind === 'process') facts.push('a program on this machine (not a container)');
  else if (c.kind === 'remote') facts.push('another machine');
  if (c.compose_project) facts.push(`compose project ${c.compose_project}`);
  if (c.image) facts.push(`image ${c.image}`);
  if (c.state) facts.push(c.state);
  if (c.data_mount) facts.push(`data: ${describeDataMount(c.data_mount)}`);
  return facts;
}

/** The VCO-data line for a candidate. */
export function vcoDataLine(service: CoreServiceName, c: EndpointCandidate): string {
  if (!c.vco_data) return 'VCO data: not checked';
  if (!c.vco_data.found) {
    return service === 'weaviate' ? 'No VCO collections yet' : service === 'ollama' ? 'No VCO models yet' : 'No VCO data';
  }
  return c.vco_data.summary ? `VCO data: ${c.vco_data.summary}` : 'Holds VCO data';
}

// ─── from the detector's reply to the dialog's report ─────────────────

/** Where each service keeps its data inside the container. */
const DATA_DESTINATIONS: Record<CoreServiceName, string> = {
  weaviate: '/var/lib/weaviate',
  ollama: '/root/.ollama',
  code_embed: '/cache',
};

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

function sameHost(a: string, b: string): boolean {
  return a === b || (LOCAL_HOSTS.has(a) && LOCAL_HOSTS.has(b));
}

/** Is `d` what `row` points at? */
function isCurrent(d: DetectedCandidate, row: ServiceEndpointRow | null | undefined): boolean {
  if (!row || (row.mode === 'vco_managed' && !row.enabled)) return false;
  if (row.mode === 'adopted_container') return !!d.container && d.container.name === row.container_name;
  return sameHost(d.host, row.host) && d.port === row.port;
}

function dataMountOf(service: CoreServiceName, c: DetectedContainer | null | undefined): DataMount | null {
  const mounts = c?.mounts ?? [];
  return mounts.find((m) => m.destination === DATA_DESTINATIONS[service]) ?? null;
}

function toCandidate(service: CoreServiceName, d: DetectedCandidate, row: ServiceEndpointRow | null | undefined): EndpointCandidate {
  const markers = d.vco_markers ?? [];
  const summary = markers.length === 0 ? null : markers.length > 3 ? `${markers.slice(0, 3).join(', ')} +${markers.length - 3}` : markers.join(', ');
  const c = d.container ?? null;
  return {
    kind: c ? 'container' : LOCAL_HOSTS.has(d.host) ? 'process' : 'remote',
    container_name: c?.name ?? null,
    compose_project: c?.compose_project ?? null,
    image: c?.image ?? null,
    state: c?.state ?? (d.live ? 'answering' : 'not answering'),
    url: d.url,
    port: d.port,
    grpc_port: d.grpc_port ?? null,
    vco_data: d.has_vco_data === undefined ? null : { found: d.has_vco_data, summary },
    compatible: d.compatible !== false,
    incompatible_reason: d.compatible === false ? (d.reason ?? null) : null,
    data_mount: dataMountOf(service, c),
    current: isCurrent(d, row),
  };
}

/** Why `service` waits for a choice, in the user's words. */
export function pendingReason(service: CoreServiceName, row: ServiceEndpointRow | null | undefined): string {
  if (service === 'weaviate' && row && row.mode === 'vco_managed' && !row.enabled) {
    return 'VCO found a Weaviate it did not start. It will not use it — or start a second one next to it — until you choose.';
  }
  return `Where ${serviceLabel(service)} runs is not recorded yet.`;
}

/**
 * The dialog's report from the detector's reply, each service's row, and the
 * services waiting for a choice. Marks the candidate the row points at
 * (`current`) and recommends one: a usable instance already holding VCO
 * data, else the first usable live one. Pure.
 */
export function buildCandidateReport(
  detection: DetectionReport | null,
  rows: Partial<Record<CoreServiceName, ServiceEndpointRow | null>>,
  pending: readonly CoreServiceName[],
): CandidateReport {
  const services: Partial<Record<CoreServiceName, ServiceCandidates>> = {};
  for (const service of CORE_SERVICES) {
    const row = rows[service] ?? null;
    const detected = detection?.candidates?.[service];
    if (!detected && !pending.includes(service)) continue;
    const candidates = (detected ?? []).map((d) => toCandidate(service, d, row));
    const usable = candidates.filter((c) => c.compatible && !c.current && c.state !== 'not answering');
    const pick = usable.find((c) => c.vco_data?.found === true) ?? usable[0];
    if (pick) pick.recommended = true;
    const isPending = pending.includes(service);
    services[service] = {
      row,
      pending_consent: isPending,
      reason: isPending ? pendingReason(service, row) : null,
      candidates,
    };
  }
  return { ok: !detection?.error, services, error: detection?.error ?? null };
}

/** The rows keyed by service, from a `services_status` snapshot. */
export function rowsFromSnapshot(
  snapshot: ServicesRuntimeSnapshot | null,
): Partial<Record<CoreServiceName, ServiceEndpointRow | null>> {
  const rows: Partial<Record<CoreServiceName, ServiceEndpointRow | null>> = {};
  for (const s of snapshot?.services ?? []) {
    if (isCoreService(s.name)) rows[s.name] = s.endpoint;
  }
  return rows;
}

/** The services a snapshot says wait for a choice. */
export function pendingFromSnapshot(snapshot: ServicesRuntimeSnapshot | null): CoreServiceName[] {
  return CORE_SERVICES.filter((n) => snapshot?.services.find((s) => s.name === n)?.pending_choice === true);
}

// ─── Tauri commands ────────────────────────────────────────────────────

export function getServicesStatus(): Promise<ServicesRuntimeSnapshot> {
  return invoke<ServicesRuntimeSnapshot>('services_status');
}

/** The detector's raw reply (turn it into a report with {@link buildCandidateReport}). */
export function getEndpointCandidates(service?: CoreServiceName): Promise<DetectionReport> {
  return invoke<DetectionReport>('services_endpoint_candidates', { service: service ?? null });
}

export function runEndpointAction(action: EndpointAction): Promise<Record<string, unknown>> {
  return invoke<Record<string, unknown>>('services_endpoint_action', { action });
}
