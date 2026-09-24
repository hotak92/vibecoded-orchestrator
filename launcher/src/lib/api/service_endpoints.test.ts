// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 (service endpoints SSOT) — the Services page's and the adoption
// dialog's decisions, pinned against the pure functions they render from.
// The wiring (that the page and dialog actually render these) is pinned by
// `routes/services/services-page.wiring.test.ts`.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  ACTION_LABELS,
  adoptActionFor,
  adoptNeedsKgConfirm,
  buildCandidateReport,
  CORE_SERVICES,
  describeDataMount,
  modeBadge,
  parseDataMount,
  pendingServices,
  rowUrl,
  serviceActions,
  vcoCopyAction,
  vcoCopyNeedsKgConfirm,
  type CandidateReport,
  type DetectionReport,
  type EndpointMode,
  type ServiceEndpointRow,
  type ServiceRuntimeState,
} from './service_endpoints';

function row(service: string, mode: EndpointMode, extra: Partial<ServiceEndpointRow> = {}): ServiceEndpointRow {
  return {
    service,
    mode,
    scheme: 'http',
    host: 'localhost',
    port: 8081,
    grpc_port: service === 'weaviate' ? 50052 : null,
    container_name: null,
    compose_project: null,
    data_mount_json: null,
    enabled: true,
    autostart: true,
    source: 'install_probe',
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
    port: endpoint?.port ?? 8081,
    url: '',
    externally_managed: (endpoint?.mode ?? 'vco_managed') !== 'vco_managed',
    mode: endpoint?.mode ?? 'vco_managed',
    endpoint,
    container_name: endpoint?.container_name ?? null,
    zombie: false,
    ...extra,
  };
}

describe('the three modes render three badges', () => {
  it('VCO-managed / your container / an URL / not recorded', () => {
    expect(modeBadge(state('code_embed', row('code_embed', 'vco_managed'))).label).toBe('VCO-managed');
    expect(
      modeBadge(state('weaviate', row('weaviate', 'adopted_container', { container_name: 'their_weaviate' }))).label,
    ).toBe('Using your container their_weaviate');
    expect(
      modeBadge(state('ollama', row('ollama', 'adopted_external', { host: 'gpu.lan', port: 11434 }))).label,
    ).toBe('Using http://gpu.lan:11434');
    expect(modeBadge(state('ollama', null)).tone).toBe('pending');
  });

  it('renders a URL the way the Rust/Python mirror does (default port omitted)', () => {
    expect(rowUrl(row('ollama', 'adopted_external', { scheme: 'https', host: 'o.example', port: 443 }))).toBe(
      'https://o.example',
    );
    expect(rowUrl(row('ollama', 'adopted_external', { host: '[::1]', port: 11434 }))).toBe('http://[::1]:11434');
  });
});

describe('row actions', () => {
  // SE-4 red-proof (9): no mode ever offers "Refuse" or "Reset".
  it('never offers refuse or reset, in any mode', () => {
    const labels = Object.values(ACTION_LABELS).map((l) => l.toLowerCase());
    expect(labels.some((l) => l.includes('refuse') || l.includes('reset'))).toBe(false);
    for (const mode of ['vco_managed', 'adopted_container', 'adopted_external'] as EndpointMode[]) {
      for (const name of CORE_SERVICES) {
        if (name === 'code_embed' && mode !== 'vco_managed') continue;
        const acts = serviceActions(state(name, row(name, mode, { container_name: 'c' }), { zombie: true }));
        expect(acts.map(String)).not.toContain('refuse');
        expect(acts.map(String)).not.toContain('reset');
      }
    }
  });

  it('VCO-managed: lifecycle + Change… (Weaviate/Ollama); code-embed has no Change…', () => {
    expect(serviceActions(state('weaviate', row('weaviate', 'vco_managed')))).toEqual([
      'start',
      'stop',
      'restart',
      'change',
    ]);
    expect(serviceActions(state('code_embed', row('code_embed', 'vco_managed')))).toEqual([
      'start',
      'stop',
      'restart',
    ]);
  });

  it('an adopted container gets lifecycle by name; "Let VCO manage it" only where the snapshot offers it', () => {
    // Offered (Rust `hand_to_vco_offered`: VCO's compose name).
    const offered = state('ollama', row('ollama', 'adopted_container', { container_name: 'vco_ollama' }), {
      hand_to_vco_offered: true,
    });
    expect(serviceActions(offered)).toEqual(['start', 'stop', 'restart', 'change', 'hand_to_vco']);
    // Not offered: a container under another name (the verb would refuse it).
    const foreign = state('ollama', row('ollama', 'adopted_container', { container_name: 'legacy_ollama' }), {
      hand_to_vco_offered: false,
    });
    expect(serviceActions(foreign)).toEqual(['start', 'stop', 'restart', 'change']);
    // An older snapshot without the field offers nothing.
    const legacy = state('ollama', row('ollama', 'adopted_container', { container_name: 'vco_ollama' }));
    expect(serviceActions(legacy)).not.toContain('hand_to_vco');
  });

  it('the page never re-derives the hand-over rule from the row', () => {
    // A row that LOOKS admissible is not offered unless the server says so,
    // and the server's word is followed — the rule lives in Rust only.
    const s = state('weaviate', row('weaviate', 'adopted_container', { container_name: 'vco_weaviate' }), {
      hand_to_vco_offered: false,
    });
    expect(serviceActions(s)).not.toContain('hand_to_vco');
    const url = state('weaviate', row('weaviate', 'adopted_external'), { hand_to_vco_offered: true });
    expect(serviceActions(url)).toContain('hand_to_vco');
  });

  it('an adopted URL has no lifecycle — only Change…', () => {
    expect(serviceActions(state('ollama', row('ollama', 'adopted_external')))).toEqual(['change']);
  });

  it('a service waiting for your choice offers only the choice (no Start on a disabled row)', () => {
    const pending = state('weaviate', row('weaviate', 'vco_managed', { enabled: false }), { pending_choice: true });
    expect(serviceActions(pending)).toEqual(['change']);
  });

  it('a stuck container offers Recover', () => {
    expect(serviceActions(state('code_embed', row('code_embed', 'vco_managed'), { zombie: true }))).toContain('recover');
  });
});

describe('data mounts', () => {
  it('parses and describes bind and volume mounts; refuses garbage', () => {
    const vol = parseDataMount('{"kind":"volume","source":"vco_weaviate_data","destination":"/var/lib/weaviate"}');
    expect(describeDataMount(vol)).toBe('volume vco_weaviate_data → /var/lib/weaviate');
    const bind = parseDataMount('{"kind":"bind","source":"/data/models","destination":"/root/.ollama"}');
    expect(describeDataMount(bind)).toBe('folder /data/models → /root/.ollama');
    expect(parseDataMount('not json')).toBeNull();
    expect(parseDataMount('{"kind":"tmpfs","source":"x"}')).toBeNull();
    expect(describeDataMount(null)).toBe('not recorded');
  });
});

describe('the adoption dialog', () => {
  const report: CandidateReport = {
    ok: true,
    services: {
      weaviate: {
        row: null,
        pending_consent: true,
        candidates: [{ url: 'http://localhost:8080', container_name: 'their_weaviate', compatible: true }],
      },
      ollama: { row: row('ollama', 'adopted_external', { port: 11434 }), pending_consent: false, candidates: [] },
    },
  };

  it('asks only about the services the detector marks pending', () => {
    expect(pendingServices(report)).toEqual(['weaviate']);
    expect(pendingServices(null)).toEqual([]);
  });

  it('"Use this one" adopts the container by name, else the URL', () => {
    expect(adoptActionFor('weaviate', { url: 'http://localhost:8080', container_name: 'their_weaviate' })).toEqual({
      action: 'adopt',
      service: 'weaviate',
      container: 'their_weaviate',
    });
    expect(adoptActionFor('ollama', { url: 'http://localhost:11434', kind: 'process' })).toEqual({
      action: 'adopt',
      service: 'ollama',
      url: 'http://localhost:11434',
    });
  });

  it('leaving a Weaviate that holds VCO data needs the KG confirmation (I6)', () => {
    const current = {
      row: row('weaviate', 'adopted_container', { container_name: 'w' }),
      candidates: [{ url: 'http://localhost:8081', current: true, vco_data: { found: true } }],
    };
    expect(vcoCopyNeedsKgConfirm('weaviate', current)).toBe(true);
    const empty = { ...current, candidates: [{ url: 'x', current: true, vco_data: { found: false } }] };
    expect(vcoCopyNeedsKgConfirm('weaviate', empty)).toBe(false);
    // Unknown current endpoint: ask.
    expect(vcoCopyNeedsKgConfirm('weaviate', { ...current, candidates: [] })).toBe(true);
    // No row yet (nothing to leave) and Ollama never need it.
    expect(vcoCopyNeedsKgConfirm('weaviate', { row: null, candidates: [] })).toBe(false);
    expect(vcoCopyNeedsKgConfirm('ollama', current)).toBe(false);
    expect(vcoCopyAction('weaviate', true)).toEqual({ action: 'use_vco_copy', service: 'weaviate', accept_empty_kg: true });
    expect(vcoCopyAction('ollama', false)).toEqual({ action: 'use_vco_copy', service: 'ollama' });
  });
});

describe('from the detector’s reply to the dialog', () => {
  const detection: DetectionReport = {
    schema: 1,
    candidates: {
      weaviate: [
        {
          service: 'weaviate',
          url: 'http://localhost:8080',
          host: 'localhost',
          port: 8080,
          grpc_port: 50051,
          live: true,
          has_vco_data: false,
          vco_markers: [],
          compatible: true,
          reason: '',
          container: {
            name: 'their_weaviate',
            image: 'semitechnologies/weaviate:1.28',
            state: 'running',
            compose_project: 'theirs',
            mounts: [{ kind: 'volume', source: 'their_data', destination: '/var/lib/weaviate' }],
          },
        },
        {
          service: 'weaviate',
          url: 'http://localhost:8082',
          host: 'localhost',
          port: 8082,
          live: true,
          has_vco_data: false,
          compatible: false,
          reason: 'version 1.19 is below 1.24',
        },
      ],
      ollama: [
        { service: 'ollama', url: 'http://127.0.0.1:11434', host: '127.0.0.1', port: 11434, live: true, has_vco_data: true, vco_markers: ['qwen3-embedding:0.6b'], compatible: true },
      ],
    },
  };
  const pendingWeaviate = row('weaviate', 'vco_managed', { enabled: false });

  it('maps candidates, asks about the pending service, recommends a usable one', () => {
    const r = buildCandidateReport(detection, { weaviate: pendingWeaviate, ollama: row('ollama', 'adopted_external', { host: 'localhost', port: 11434 }) }, ['weaviate']);
    expect(pendingServices(r)).toEqual(['weaviate']);
    const w = r.services.weaviate!;
    expect(w.reason).toMatch(/did not start/);
    expect(w.candidates[0]).toMatchObject({
      kind: 'container',
      container_name: 'their_weaviate',
      compose_project: 'theirs',
      compatible: true,
      recommended: true,
      current: false,
      data_mount: { kind: 'volume', source: 'their_data', destination: '/var/lib/weaviate' },
    });
    expect(w.candidates[1]).toMatchObject({ kind: 'process', compatible: false, incompatible_reason: 'version 1.19 is below 1.24' });
    // The Ollama row points at 11434 on localhost == 127.0.0.1: that one is in use.
    expect(r.services.ollama!.candidates[0].current).toBe(true);
    expect(r.services.ollama!.pending_consent).toBe(false);
  });

  it('the placeholder "confirmation pending" row holds no data to leave', () => {
    const r = buildCandidateReport(detection, { weaviate: pendingWeaviate }, ['weaviate']);
    const w = r.services.weaviate!;
    expect(vcoCopyNeedsKgConfirm('weaviate', w)).toBe(false);
    expect(adoptNeedsKgConfirm('weaviate', w, w.candidates[0])).toBe(false);
  });

  it('leaving an adopted Weaviate that holds VCO data for an empty one needs the KG confirmation', () => {
    const current = row('weaviate', 'adopted_container', { container_name: 'vco_weaviate', port: 8081 });
    const withData: DetectionReport = {
      candidates: {
        weaviate: [
          { service: 'weaviate', url: 'http://localhost:8081', host: 'localhost', port: 8081, has_vco_data: true, compatible: true, container: { name: 'vco_weaviate' } },
          ...(detection.candidates!.weaviate ?? []),
        ],
      },
    };
    const w = buildCandidateReport(withData, { weaviate: current }, []).services.weaviate!;
    expect(w.candidates[0].current).toBe(true);
    expect(adoptNeedsKgConfirm('weaviate', w, w.candidates[1])).toBe(true);
    expect(adoptActionFor('weaviate', w.candidates[1], true)).toEqual({
      action: 'adopt',
      service: 'weaviate',
      container: 'their_weaviate',
      accept_empty_kg: true,
    });
  });

  it('a failed detection still reports the pending services (VCO’s own copy stays offered)', () => {
    const r = buildCandidateReport({ error: 'no runtime' }, { weaviate: null }, ['weaviate']);
    expect(r.ok).toBe(false);
    expect(r.error).toBe('no runtime');
    expect(r.services.weaviate!.candidates).toEqual([]);
    expect(pendingServices(r)).toEqual(['weaviate']);
  });

  it('the Weaviate confirmation-pending row shows as waiting, not as disabled', () => {
    expect(modeBadge(state('weaviate', pendingWeaviate, { pending_choice: true })).label).toBe('Waiting for your choice');
  });
});

describe('the service list is the shared parity table’s', () => {
  // The Rust/Python resolvers both execute tests/fixtures/service_endpoint_parity.json;
  // the frontend's service list is pinned to the same data.
  const table = JSON.parse(
    readFileSync(fileURLToPath(new URL('../../../../tests/fixtures/service_endpoint_parity.json', import.meta.url)), 'utf8'),
  );

  it('CORE_SERVICES matches the parity table', () => {
    expect([...CORE_SERVICES].sort()).toEqual(Object.keys(table.constants.default_ports).sort());
  });

  // O-A3 (v0.2.97 review round 7): rowUrl claims "the same render as the
  // Rust/Python mirror" — so it runs the SAME render_cases the Rust and
  // Python resolvers execute. Two inline cases cannot see a divergence that
  // would show on the Services page and be caught by nothing.
  it('rowUrl renders every parity render_case the way the mirror does', () => {
    for (const c of table.render_cases as Array<Record<string, unknown>>) {
      const spec = c.row as Record<string, unknown>;
      const r = row(
        c.service as string,
        spec.mode as EndpointMode,
        {
          scheme: (spec.scheme as string) ?? 'http',
          host: (spec.host as string) ?? 'localhost',
          port: spec.port as number,
          grpc_port: (spec.grpc_port as number | undefined) ?? null,
          container_name: (spec.container_name as string | undefined) ?? null,
        },
      );
      expect(rowUrl(r), `case "${c.name}"`).toBe(c.expect_url as string);
    }
  });
});
