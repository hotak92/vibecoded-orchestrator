// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 (R7b F22) — "Move to another port" on the Services page.
//
// VCO's OWN (`vco_managed`) Weaviate / Ollama / code-embed can be moved to
// another host port: the Rust command `services_endpoint_action` (its `Move`
// variant — the one builder of a service-endpoint verb argv) validates the
// request and runs the rows' one writer,
// `python -m vco_lib.service_endpoints move --service S --port N [--grpc-port G]`
// (the container is re-created on the new port with the SAME data, checked
// before and after, rolled back on failure; every project's env and the MCP
// registration follow). An ADOPTED service is somebody else's: VCO follows
// the port its owner publishes and offers no move.
//
// Every decision is a pure function here so vitest can pin it; the page
// (`routes/services/+page.svelte`) is markup and wiring.

import { runEndpointAction } from '$lib/api/service_endpoints';
import type { EndpointAction, ServiceRuntimeState } from '$lib/api/service_endpoints';

/** The services the page may move (Rust `CoreService`). */
export type MovableService = 'weaviate' | 'ollama' | 'code_embed';

/** Must match `lifecycle::MIN_SERVICE_PORT` (the privileged range is refused). */
export const MIN_MOVE_PORT = 1024;
export const MAX_PORT = 65535;

/** What the page shows in a row's actions for moving it. */
export type MoveOffer =
  /** A "Move to another port…" button. */
  | { kind: 'move'; service: MovableService }
  /** No button: a note saying VCO follows the owner's port. */
  | { kind: 'follows_owner'; note: string }
  /** Nothing (not a core service, no row recorded yet, or a choice pending). */
  | { kind: 'none' };

function asMovable(name: string): MovableService | null {
  return name === 'weaviate' || name === 'ollama' || name === 'code_embed' ? name : null;
}

/** The move affordance for one Services-page row. */
export function moveOffer(svc: ServiceRuntimeState): MoveOffer {
  const service = asMovable(svc.name);
  const row = svc.endpoint;
  if (service === null || row === null || svc.pending_choice) return { kind: 'none' };
  switch (row.mode) {
    case 'vco_managed':
      return { kind: 'move', service };
    case 'adopted_container':
      return {
        kind: 'follows_owner',
        note:
          `This is your container ${row.container_name ?? ''}`.trimEnd() +
          '. VCO does not move it: publish it on another port where it is managed, and VCO follows the port it publishes.',
      };
    case 'adopted_external':
      return {
        kind: 'follows_owner',
        note: 'VCO only connects to this endpoint and does not move it. If its owner moves it, use Change… to point VCO at the new address.',
      };
  }
}

/**
 * Check a typed port: `null` when acceptable, else a one-line reason.
 * `current` is the service's port now (moving to it would change nothing).
 */
export function checkMovePort(raw: string, current: number | null): string | null {
  const text = raw.trim();
  if (!/^[0-9]+$/.test(text)) return 'Enter a port number.';
  const port = Number(text);
  if (port < MIN_MOVE_PORT || port > MAX_PORT) {
    return `Pick a port from ${MIN_MOVE_PORT} to ${MAX_PORT}.`;
  }
  if (current !== null && port === current) return 'That is the port it uses now.';
  return null;
}

/**
 * Check an optional gRPC port (Weaviate only): empty = keep its offset from
 * the HTTP port. `null` when acceptable.
 */
export function checkGrpcPort(raw: string, httpPort: string): string | null {
  const text = raw.trim();
  if (text === '') return null;
  if (!/^[0-9]+$/.test(text)) return 'Enter a port number, or leave it empty.';
  const port = Number(text);
  if (port < MIN_MOVE_PORT || port > MAX_PORT) {
    return `Pick a port from ${MIN_MOVE_PORT} to ${MAX_PORT}.`;
  }
  if (text === httpPort.trim()) return 'The gRPC port must differ from the HTTP port.';
  return null;
}

/** A checked move request (turned into the `move` {@link EndpointAction} by {@link moveAction}). */
export interface MoveRequest {
  service: MovableService;
  port: number;
  grpcPort: number | null;
}

/** Build the request from checked input (call the checks first). */
export function moveRequest(service: MovableService, port: string, grpcPort: string): MoveRequest {
  const grpc = service === 'weaviate' && grpcPort.trim() !== '' ? Number(grpcPort.trim()) : null;
  return { service, port: Number(port.trim()), grpcPort: grpc };
}

/** The verb's reply: `{ ok: true, output: "<its lines>" }`. */
export interface MoveResult {
  ok: boolean;
  output?: string;
}

/** The `services_endpoint_action` payload for a move (Rust `EndpointAction::Move`). */
export function moveAction(req: MoveRequest): EndpointAction {
  return { action: 'move', service: req.service, port: req.port, grpc_port: req.grpcPort };
}

/** Run the move (the Rust command re-validates and builds the argv). */
export async function moveService(req: MoveRequest): Promise<MoveResult> {
  return (await runEndpointAction(moveAction(req))) as unknown as MoveResult;
}

/** The verb's output (or error text) as display lines, blanks dropped. */
export function resultLines(text: string | undefined | null): string[] {
  return (text ?? '')
    .split(/\r?\n/)
    .map((l) => l.trimEnd())
    .filter((l) => l.trim() !== '');
}
