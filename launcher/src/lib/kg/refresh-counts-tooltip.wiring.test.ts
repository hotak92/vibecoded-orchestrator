// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the "Refresh counts" tooltips (v0.2.105 fix, review
// NIT). The button live-counts EVERY collection on the machine, and a
// live Aggregate lazy-loads a class into Weaviate's RAM for good
// (Weaviate 1.28 never unloads it) — a cost the user pays on every
// press. The tooltip must SAY so: before the fix it read like a free
// cache refill. This file pins the disclosure on BOTH surfaces (the KG
// collections view and the /codegraph page) so it cannot silently
// regress to the cost-free wording.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

function refreshTooltip(source: string, where: string): string {
  const line = source
    .split('\n')
    .find((l) => l.includes('title="Count every collection'));
  expect(line, `no "Count every collection" tooltip found in ${where}`).toBeTruthy();
  const m = line?.match(/title="([^"]*)"/);
  expect(m, `no title attribute parsed in ${where}`).toBeTruthy();
  return m![1];
}

describe('"Refresh counts" tooltip discloses the Weaviate-memory cost', () => {
  it('the KG collections view warns that every collection is loaded into memory', () => {
    const src = readFileSync(resolve(here, 'CollectionList.svelte'), 'utf8');
    expect(refreshTooltip(src, 'CollectionList.svelte')).toContain(
      "Weaviate's memory",
    );
  });

  it('the /codegraph page warns that every collection is loaded into memory', () => {
    const src = readFileSync(
      resolve(here, '../../routes/codegraph/+page.svelte'),
      'utf8',
    );
    expect(refreshTooltip(src, 'routes/codegraph/+page.svelte')).toContain(
      "Weaviate's memory",
    );
  });
});
