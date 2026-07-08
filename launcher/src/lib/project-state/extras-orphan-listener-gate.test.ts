// SPDX-License-Identifier: AGPL-3.0-or-later
//
// P1a (v0.2.75 / B-4): grep-gate ensuring the orphan Tauri event
// `module://codegraph-extras-sync-progress` — which had a listener in
// ExtraCodegraphPathsPanel.svelte but ZERO emit sites anywhere in the
// codebase — stays deleted. The listener was speculative wiring: each
// mutation path already calls `load()` afterwards, so the auto-refresh
// was redundant, and the hedging comment invited a future editor to
// "finish" wiring an event that is never emitted.
//
// This test walks the entire `launcher/src` tree and fails if the event
// string reappears in ANY .svelte / .ts / .js source. Kill the string,
// not just the listener.

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const ORPHAN_EVENT = 'module://codegraph-extras-sync-progress';

// This file lives at launcher/src/lib/project-state/ — walk up to `src`.
const HERE = dirname(fileURLToPath(import.meta.url));
const SRC_ROOT = resolve(HERE, '..', '..');

const SOURCE_EXTS = ['.svelte', '.ts', '.js'] as const;

function walk(dir: string, out: string[]): void {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      // Skip build/output dirs that may contain generated copies.
      if (entry === 'node_modules' || entry === '.svelte-kit' || entry === 'build') {
        continue;
      }
      walk(full, out);
    } else if (SOURCE_EXTS.some((ext) => entry.endsWith(ext))) {
      // Don't count this gate file itself (it names the string on purpose).
      if (full === fileURLToPath(import.meta.url)) continue;
      out.push(full);
    }
  }
}

describe('P1a orphan listener removal', () => {
  it('no source under launcher/src references the orphan sync-progress event', () => {
    const files: string[] = [];
    walk(SRC_ROOT, files);
    const offenders = files.filter((f) =>
      readFileSync(f, 'utf-8').includes(ORPHAN_EVENT),
    );
    expect(
      offenders,
      `orphan event string "${ORPHAN_EVENT}" must not appear in any source ` +
        `(found in: ${offenders.join(', ')})`,
    ).toEqual([]);
  });
});
