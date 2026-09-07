// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.92 (review MAJOR-8, Svelte half) — pin the Services page's service
// union to the hub's canonical service table.
//
// Four hand-maintained service lists survive in this repo
// (`vct-hub/src/lifecycle_api.rs`, `infra_watchdog.rs`,
// `commands/lifecycle.rs`, `storage_ux.rs`); the Rust divergences between them
// are documented and carry pinning tests. The Svelte one did not — it was
// hand-listed with nothing asserting it, so a service added hub-side would
// reach this page as a `ContainerFullness` arm that does not exist, and the
// picker would render "probe failed" for a container that probed perfectly.
//
// The plan allowed generation OR a pin; this is the pin — cheaper, and it
// fails at the moment of divergence rather than regenerating over it.
//
// Read as TEXT rather than by importing: vitest here runs a pure-node
// environment with no SvelteKit plugin (see `launcher/vitest.config.ts`), so
// a `.svelte` file cannot be imported. The same source-level technique
// `kg-sync-banner-logic.test.ts` uses for its component pins.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** launcher/src/routes/services → routes → src → launcher → repo root. */
const REPO_ROOT = fileURLToPath(new URL('../../../../', import.meta.url));

const HUB_SRC = `${REPO_ROOT}launcher/src-tauri/vct-hub/src/lifecycle_api.rs`;
const PAGE_SRC = `${REPO_ROOT}launcher/src/routes/services/+page.svelte`;

/**
 * Services the hub reports from `/services/status` — the `(name, port, url)`
 * tuples inside `canonical_service_skeletons()`.
 */
export function hubServiceNames(rustSource: string): string[] {
  const start = rustSource.indexOf('fn canonical_service_skeletons()');
  if (start < 0) throw new Error('canonical_service_skeletons() not found');
  // The tuple array ends at the `]` that closes it, before `.iter()`.
  const end = rustSource.indexOf('.iter()', start);
  if (end < 0) throw new Error('tuple array terminator not found');
  const body = rustSource.slice(start, end);
  return [...body.matchAll(/\(\s*"([a-z_]+)"\s*,\s*\d+u?\d*/g)].map((m) => m[1]);
}

/** The `kind: '<service>'` arms of the page's `ContainerFullness` union. */
export function pageUnionKinds(svelteSource: string): string[] {
  const start = svelteSource.indexOf('type ContainerFullness =');
  if (start < 0) throw new Error('ContainerFullness union not found');
  const end = svelteSource.indexOf('\n\n', start);
  const body = svelteSource.slice(start, end < 0 ? undefined : end);
  return [...body.matchAll(/kind:\s*'([a-z_]+)'/g)].map((m) => m[1]);
}

/**
 * Hub services this page deliberately does not give a fullness arm.
 *
 * NOT a drift allowance — a container/process boundary. `model_gateway` is a
 * plain process: no image, no adoption mode, nothing to re-detect, so there is
 * no candidate list for a fullness probe to describe. The page renders it as
 * its own card instead. Adding an arm for it would be the mistake, which is
 * why the exclusion is named here rather than left as an unexplained gap.
 */
const NON_CONTAINER_SERVICES = ['model_gateway'];

describe('Services page union ↔ hub canonical service table (review MAJOR-8)', () => {
  const hub = hubServiceNames(readFileSync(HUB_SRC, 'utf-8'));
  const page = pageUnionKinds(readFileSync(PAGE_SRC, 'utf-8'));

  it('reads both lists (guards the extractors themselves)', () => {
    // A pin whose readers silently returned [] would pass forever.
    expect(hub).toContain('weaviate');
    expect(hub.length).toBeGreaterThanOrEqual(4);
    expect(page).toContain('weaviate');
    expect(page.length).toBeGreaterThanOrEqual(3);
  });

  it('covers every containerised hub service, and nothing the hub does not serve', () => {
    const expected = hub.filter((s) => !NON_CONTAINER_SERVICES.includes(s));
    const missingFromPage = expected.filter((s) => !page.includes(s));
    const notServedByHub = page.filter((s) => !hub.includes(s));
    expect({ missingFromPage, notServedByHub }).toEqual({
      missingFromPage: [],
      notServedByHub: [],
    });
  });

  it('every declared exclusion is a service the hub actually serves', () => {
    // Keeps the exclusion list from outliving its subject: a stale name here
    // would silently widen the allowance for some future service.
    for (const s of NON_CONTAINER_SERVICES) expect(hub).toContain(s);
  });

  it('the model gateway stays a card, not a fullness arm', () => {
    expect(page).not.toContain('model_gateway');
  });
});

describe('extractors', () => {
  it('reads the hub tuple list without picking up neighbouring code', () => {
    const src = `
fn canonical_service_skeletons() -> Vec<ServiceRuntimeState> {
    [
        ("alpha", 1u16, "http://localhost:1/x"),
        ("beta_two", 2u16, "http://127.0.0.1:2/y"),
    ]
    .iter()
    .map(|(name, port, url)| ServiceRuntimeState { ("ignored", 9u16, "") })
    .collect()
}`;
    expect(hubServiceNames(src)).toEqual(['alpha', 'beta_two']);
  });

  it('reads only the union arms, not later `kind:` uses in the file', () => {
    const src = `
  type ContainerFullness =
    | { kind: 'alpha'; a: number }
    | { kind: 'beta_two'; b: number };

  function f(c: X) { if (c.kind === 'gamma') return; const k = { kind: 'delta' }; }
`;
    expect(pageUnionKinds(src)).toEqual(['alpha', 'beta_two']);
  });
});
