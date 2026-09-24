// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 (R7b F22) — the Services page's "Move to another port": which rows
// get the button, the port checks, and the EXACT request the page sends. The
// request is the `move` variant of `services_endpoint_action` — the Rust
// `lifecycle::endpoint_action_argv` is the one builder of a service-endpoint
// verb argv (pinned there against the real `vco_lib.service_endpoints`
// parser). Tauri bridge mocked.

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({ invoke: vi.fn(), tauriAvailable: vi.fn(() => true) }));

import { invoke } from '$lib/tauri';
import type { EndpointMode, ServiceEndpointRow, ServiceRuntimeState } from '$lib/api/service_endpoints';
import {
  MIN_MOVE_PORT,
  checkGrpcPort,
  checkMovePort,
  moveOffer,
  moveRequest,
  moveService,
  resultLines,
} from './service-endpoint-move';

const invokeMock = invoke as unknown as ReturnType<typeof vi.fn>;

function row(service: string, mode: EndpointMode, extra: Partial<ServiceEndpointRow> = {}): ServiceEndpointRow {
  return {
    service,
    mode,
    scheme: 'http',
    host: 'localhost',
    port: 11440,
    grpc_port: null,
    container_name: null,
    compose_project: null,
    data_mount_json: null,
    enabled: true,
    autostart: true,
    source: 'install',
    confirmed_by_user: false,
    verified_at: null,
    updated_at: 0,
    ...extra,
  };
}

function state(name: string, endpoint: ServiceEndpointRow | null, extra: Partial<ServiceRuntimeState> = {}): ServiceRuntimeState {
  return {
    name,
    running: true,
    port: endpoint?.port ?? 0,
    url: '',
    externally_managed: false,
    mode: endpoint?.mode ?? null,
    endpoint,
    container_name: endpoint?.container_name ?? null,
    ...extra,
  };
}

beforeEach(() => invokeMock.mockReset());

describe('which rows offer "Move to another port"', () => {
  it('every VCO-managed core service gets the button', () => {
    for (const name of ['weaviate', 'ollama', 'code_embed']) {
      expect(moveOffer(state(name, row(name, 'vco_managed')))).toEqual({ kind: 'move', service: name });
    }
  });

  it('an adopted row gets NO button — it says VCO follows the owner’s port', () => {
    const container = moveOffer(state('ollama', row('ollama', 'adopted_container', { container_name: 'their-ollama' })));
    expect(container.kind).toBe('follows_owner');
    expect(container.kind === 'follows_owner' && container.note).toContain('their-ollama');
    expect(container.kind === 'follows_owner' && container.note).toContain('follows the port');
    const external = moveOffer(state('weaviate', row('weaviate', 'adopted_external', { host: 'gpu-box' })));
    expect(external.kind).toBe('follows_owner');
  });

  it('nothing for a row not recorded yet, a pending choice, or a non-core service', () => {
    expect(moveOffer(state('weaviate', null))).toEqual({ kind: 'none' });
    expect(moveOffer(state('weaviate', row('weaviate', 'vco_managed'), { pending_choice: true }))).toEqual({
      kind: 'none',
    });
    expect(moveOffer(state('model_gateway', row('model_gateway', 'vco_managed')))).toEqual({ kind: 'none' });
  });
});

describe('port checks', () => {
  it('accepts a port in range that differs from the current one', () => {
    expect(checkMovePort('11441', 11440)).toBeNull();
    expect(checkMovePort(' 65535 ', 11440)).toBeNull();
    expect(checkMovePort(String(MIN_MOVE_PORT), null)).toBeNull();
  });

  it('refuses text, the privileged range, out of range, and the current port', () => {
    for (const bad of ['', 'abc', '11441a', '-5', '1.5', '80', '1023', '65536', '99999']) {
      expect(checkMovePort(bad, 11440), bad).not.toBeNull();
    }
    expect(checkMovePort('11440', 11440)).toContain('uses now');
  });

  it('the gRPC port is optional, in range, and not the HTTP port', () => {
    expect(checkGrpcPort('', '8091')).toBeNull();
    expect(checkGrpcPort('50062', '8091')).toBeNull();
    expect(checkGrpcPort('80', '8091')).not.toBeNull();
    expect(checkGrpcPort('8091', '8091')).not.toBeNull();
    expect(checkGrpcPort('x', '8091')).not.toBeNull();
  });
});

describe('the exact request the page sends', () => {
  it('code-embed: service + port, no gRPC', async () => {
    invokeMock.mockResolvedValue({ ok: true, output: 'code_embed moved to :11441' });
    const out = await moveService(moveRequest('code_embed', '11441', '50062'));
    expect(invokeMock).toHaveBeenCalledTimes(1);
    expect(invokeMock).toHaveBeenCalledWith('services_endpoint_action', {
      action: { action: 'move', service: 'code_embed', port: 11441, grpc_port: null },
    });
    expect(resultLines(out.output)).toEqual(['code_embed moved to :11441']);
  });

  it('Weaviate: the gRPC port when given, null (keep its offset) when empty', async () => {
    invokeMock.mockResolvedValue({ ok: true, output: '' });
    await moveService(moveRequest('weaviate', ' 8091 ', '50062'));
    expect(invokeMock).toHaveBeenLastCalledWith('services_endpoint_action', {
      action: { action: 'move', service: 'weaviate', port: 8091, grpc_port: 50062 },
    });
    await moveService(moveRequest('weaviate', '8091', ''));
    expect(invokeMock).toHaveBeenLastCalledWith('services_endpoint_action', {
      action: { action: 'move', service: 'weaviate', port: 8091, grpc_port: null },
    });
    // No second command exists for a move.
    expect(invokeMock.mock.calls.every((c) => c[0] === 'services_endpoint_action')).toBe(true);
  });

  it('result lines keep the verb’s lines and drop blanks', () => {
    expect(resultLines('a\r\n\nb  \n')).toEqual(['a', 'b']);
    expect(resultLines(undefined)).toEqual([]);
  });
});
