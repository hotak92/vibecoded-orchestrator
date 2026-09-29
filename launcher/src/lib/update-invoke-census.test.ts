// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-08, AD-1): every GUI control updates the orchestrator through
// the updater store — no component invokes an update command itself.
//
// This is a literal-name census of `invoke(...)` / `safeInvoke(...)` call
// sites over `src/**/*.{svelte,ts}` (tests and stubs excluded). The
// behavioural half — that each control's store action reaches the right
// backend call — is pinned by `stores/updater-run-kind.test.ts`; this file
// pins the other half: that nothing goes AROUND the store.
//
//   - `run_orchestrator_update` is invoked in exactly one place, the
//     orchestrator store's `runUpdate` (called only by `updater.run`);
//   - `restart_launcher` only in the updater store (`runRestart`);
//   - the retired / superseded update commands are invoked NOWHERE:
//     `update_orchestrator_at` (owner Q1), and the per-surface pipeline
//     commands the one command replaced;
//   - the root layout registers the status polling FIRST in `onMount` and
//     mounts the shell listeners (one `vct-tray-action` handler) once.
//
// The scanner itself is red-proofed by the fixture block at the top: a
// direct invoke in a component must be SEEN, a comment or string mentioning
// the name must NOT be counted.

import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = fileURLToPath(new URL('..', import.meta.url));

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === 'test-stubs' || name === 'node_modules') continue;
      walk(p, out);
    } else if (/\.(svelte|ts)$/.test(name) && !/\.test\.ts$/.test(name)) {
      out.push(p);
    }
  }
  return out;
}

/** Remove `//` and block comments (and Svelte `<!-- -->`) so a comment
 *  naming a command never counts as a call site. */
function stripComments(src: string): string {
  return src
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1');
}

// `tauriInvoke` is the orchestrator store's import alias of `invoke`.
const CALL_RE = /\b(?:safeInvoke|tauriInvoke|invoke)\s*(?:<[^()]*?>)?\s*\(\s*['"`]([a-z0-9_]+)['"`]/g;

/** Literal command names invoked in `src`, with call counts. */
function invokedNames(src: string): Map<string, number> {
  const out = new Map<string, number>();
  for (const m of stripComments(src).matchAll(CALL_RE)) {
    out.set(m[1], (out.get(m[1]) ?? 0) + 1);
  }
  return out;
}

const FILES = walk(SRC).map((abs) => ({
  rel: relative(SRC, abs).split(sep).join('/'),
  names: invokedNames(readFileSync(abs, 'utf-8')),
}));

function sitesOf(cmd: string): string[] {
  return FILES.filter((f) => f.names.has(cmd)).map((f) => f.rel);
}

describe('scanner fixtures (red-proof)', () => {
  it('sees a direct invoke, typed or not, single or double quotes', () => {
    const n = invokedNames(`
      await invoke('update_orchestrator', { path });
      await invoke<void>("restart_launcher", {});
      const x = await safeInvoke<Record<string, unknown>>('apply_pending_install');
      await tauriInvoke<UpdateOutcome | null>('run_orchestrator_update', { kind });
    `);
    expect([...n.keys()].sort()).toEqual([
      'apply_pending_install',
      'restart_launcher',
      'run_orchestrator_update',
      'update_orchestrator',
    ]);
  });

  it('does not count a comment or a bare string', () => {
    const n = invokedNames(`
      // await invoke('update_orchestrator', { path });
      /* invoke('restart_launcher') */
      <!-- invoke('apply_launcher_update') -->
      const label = 'update_orchestrator_at';
    `);
    expect(n.size).toBe(0);
  });

  it('actually scanned the launcher sources', () => {
    expect(FILES.length).toBeGreaterThan(100);
    expect(FILES.some((f) => f.rel === 'lib/stores/updater.ts')).toBe(true);
  });
});

describe('update commands are invoked only through the store', () => {
  it('run_orchestrator_update: exactly one call site, the orchestrator store', () => {
    expect(sitesOf('run_orchestrator_update')).toEqual(['lib/stores/orchestrator.ts']);
    const orch = FILES.find((f) => f.rel === 'lib/stores/orchestrator.ts')!;
    expect(orch.names.get('run_orchestrator_update')).toBe(1);
  });

  it('restart_launcher: only the updater store', () => {
    expect(sitesOf('restart_launcher')).toEqual(['lib/stores/updater.ts']);
  });

  it.each([
    'update_orchestrator_at',
    'update_orchestrator',
    'apply_launcher_update',
    'apply_pending_install',
    'resume_orchestrator_update',
    'force_resync_launcher',
    'merge_orchestrator_with_upstream',
    'rebase_orchestrator_onto_upstream',
    'check_for_launcher_update',
  ])('%s: invoked nowhere', (cmd) => {
    expect(sitesOf(cmd)).toEqual([]);
  });
});

describe('root layout wiring', () => {
  const layout = stripComments(readFileSync(join(SRC, 'routes/+layout.svelte'), 'utf-8'));

  it('startStatusPolling() is the FIRST statement of onMount (L3-F13)', () => {
    const at = layout.indexOf('onMount(() => {');
    expect(at).toBeGreaterThan(-1);
    const body = layout.slice(at + 'onMount(() => {'.length);
    const firstStatement = body.trimStart().split('\n')[0].trim();
    expect(firstStatement).toBe('const stopStatusPolling = startStatusPolling();');
  });

  it('the shell listeners (the one tray-action handler) are registered once, from the layout', () => {
    expect(layout.match(/registerShellListeners\(/g)).toHaveLength(1);
    const elsewhere = FILES.filter(
      (f) =>
        f.rel !== 'routes/+layout.svelte' &&
        f.rel !== 'lib/stores/ui.ts' &&
        /registerShellListeners\(|['"`]vct-tray-action['"`]/.test(
          stripComments(readFileSync(join(SRC, f.rel), 'utf-8')),
        ),
    ).map((f) => f.rel);
    expect(elsewhere).toEqual([]);
  });
});
