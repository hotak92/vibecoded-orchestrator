// SPDX-License-Identifier: AGPL-3.0-or-later
// The Store lists exactly the announced pair (Transcrypt, VCT Coordination),
// both "Coming soon" with no action, plus any paid module the L0 catalog
// publishes later (owner ruling 2026-11-12).
import { describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { STORE_COMING_SOON, isStoreCatalogModule, storeEntries } from './store-catalog';
import type { ModuleCatalogEntry } from './types/launcher';
import { renderSvelte } from './test-support/svelte-ssr';

function entry(over: Partial<ModuleCatalogEntry>): ModuleCatalogEntry {
  return {
    id: 'x',
    name: 'X',
    version: '1.0.0',
    description: 'desc',
    category: '',
    tags: [],
    license_required: false,
    license_variant_ids: [],
    min_orchestrator_tier: 'free',
    compatibility_hosts: [],
    is_licensed: false,
    manifest_source: 'builtin',
    kind: 'available',
    parent_id: '',
    cta_route: '',
    ...over,
  } as ModuleCatalogEntry;
}

// What `list_module_catalog` returns today: the launcher, the orchestrator
// and its subcomponents, and the bundled (free) RL reranker.
const TODAY: ModuleCatalogEntry[] = [
  entry({ id: 'vct-launcher', kind: 'bundled' }),
  entry({ id: 'orchestrator', kind: 'bundled' }),
  entry({ id: 'knowledge-graph', kind: 'subcomponent' }),
  entry({ id: 'vct-rl-reranker', name: 'RL Reranker', kind: 'installed', manifest_source: '/repo/claude_mcp_servers/rl_reranker/vct-module.json' }),
];

const PAID_L0 = entry({
  id: 'vct-future-paid',
  name: 'Future Paid',
  version: '2.1.0',
  license_required: true,
  manifest_source: 'L0:vct-future-paid',
  kind: 'available',
});

const GRID = fileURLToPath(new URL('./components/StoreGrid.svelte', import.meta.url));
const rgbFor = () => '0, 191, 166';
const noop = () => {};

describe('store catalog (owner ruling 2026-11-12)', () => {
  it('today the store lists exactly Transcrypt and VCT Coordination, both coming soon', () => {
    const got = storeEntries(TODAY);
    expect(got.map((e) => e.id)).toEqual(['transcrypt', 'vct-coordination']);
    for (const e of got) {
      expect(e.availability).toBe('coming_soon');
      expect(e.catalogKind).toBe('coming_soon');
      expect(e.version).toBe('');
    }
    expect(STORE_COMING_SOON.map((e) => e.id)).toEqual(['transcrypt', 'vct-coordination']);
  });

  it('the RL reranker is not a store item, even if an old paid L0 record lists it as bundled/free', () => {
    expect(storeEntries(TODAY).some((e) => e.id === 'vct-rl-reranker')).toBe(false);
    const freeL0 = entry({ id: 'vct-rl-reranker', manifest_source: 'L0:vct-rl-reranker', license_required: false });
    expect(isStoreCatalogModule(freeL0)).toBe(false);
  });

  it('a paid module published to L0 later still renders, after the pair', () => {
    const got = storeEntries([...TODAY, PAID_L0]);
    expect(got.map((e) => e.id)).toEqual(['transcrypt', 'vct-coordination', 'vct-future-paid']);
    const paid = got[2];
    expect(paid.availability).toBe('catalog');
    expect(paid.catalogKind).toBe('available');
    expect(paid.version).toBe('2.1.0');
    expect(paid.icon).toBe('F');
  });

  it('a catalog record for an announced id never replaces its coming-soon card', () => {
    const l0Transcrypt = entry({ id: 'transcrypt', license_required: true, manifest_source: 'L0:transcrypt' });
    const got = storeEntries([l0Transcrypt]);
    expect(got.map((e) => e.id)).toEqual(['transcrypt', 'vct-coordination']);
    expect(got[0].availability).toBe('coming_soon');
  });

  it('free, bundled and subcomponent records are never listed', () => {
    expect(isStoreCatalogModule(entry({ manifest_source: 'L0:a', license_required: true, kind: 'bundled' }))).toBe(false);
    expect(isStoreCatalogModule(entry({ manifest_source: 'L0:a', license_required: true, kind: 'subcomponent' }))).toBe(false);
    expect(isStoreCatalogModule(entry({ manifest_source: 'builtin', license_required: true }))).toBe(false);
    expect(isStoreCatalogModule(entry({ manifest_source: 'L0:a', license_required: true }))).toBe(true);
  });
});

describe('StoreGrid rendering', () => {
  it('coming-soon cards show the badge and carry no button at all', async () => {
    const html = await renderSvelte(GRID, {
      entries: storeEntries(TODAY),
      rgbFor,
      onSelect: noop,
      onOpen: noop,
    });
    expect(html).toContain('Transcrypt');
    expect(html).toContain('VCT Coordination');
    expect(html.match(/Coming soon/g)?.length).toBe(2);
    expect(html).not.toMatch(/<button/);
    expect(html).not.toMatch(/\b(Get|Buy|Install|Activate)\b/);
  });

  it('a paid L0 card renders with its one action (open the Modules page)', async () => {
    const html = await renderSvelte(GRID, {
      entries: storeEntries([PAID_L0]),
      rgbFor,
      onSelect: noop,
      onOpen: noop,
    });
    expect(html).toContain('Future Paid');
    expect(html).toContain('v2.1.0');
    expect(html.match(/<button/g)?.length).toBe(1);
    expect(html).toContain('View in Modules');
    expect(html.match(/Coming soon/g)?.length).toBe(2);
  });
});
