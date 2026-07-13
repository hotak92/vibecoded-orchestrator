// SPDX-License-Identifier: AGPL-3.0-or-later
//
// P1b (v0.2.75 / B-5): tests for the single getColorRgb home.
//
// Two contracts:
//   1. Behaviour — token present in the live CSSOM → token value;
//      token absent / no CSSOM (SSR / node) → baked fallback triplet.
//   2. One-home grep-gate — no OTHER `getColorRgb` function definition
//      exists anywhere under launcher/src (the three per-component copies
//      that this module replaced must stay deleted).

import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { getColorRgb, type BrandColor } from '$lib/color-rgb';

describe('getColorRgb — behaviour', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns the baked fallback triplet when there is no live CSSOM (node/SSR)', () => {
    // In the vitest node environment `document` is undefined → fallback path.
    expect(getColorRgb('teal')).toBe('0,191,166');
    expect(getColorRgb('purple')).toBe('123,95,255');
    expect(getColorRgb('pink')).toBe('255,79,160');
  });

  it('reads the --color-<name>-rgb token from the CSSOM when present', () => {
    // Simulate a browser: stub `document` + `getComputedStyle` so the
    // helper takes its runtime branch. A palette change in app.css would
    // surface here as a different triplet, proving the token — not a baked
    // literal — is what a live consumer reads.
    const tokens: Record<string, string> = {
      '--color-teal-rgb': '10, 20, 30',
      '--color-purple-rgb': '40, 50, 60',
      '--color-pink-rgb': '70, 80, 90',
    };
    vi.stubGlobal('document', { documentElement: {} });
    vi.stubGlobal('getComputedStyle', () => ({
      getPropertyValue: (name: string) => tokens[name] ?? '',
    }));

    // Normalized: stray spaces around the triplet are stripped.
    expect(getColorRgb('teal')).toBe('10,20,30');
    expect(getColorRgb('purple')).toBe('40,50,60');
    expect(getColorRgb('pink')).toBe('70,80,90');
  });

  it('falls back to the baked triplet when the token resolves empty', () => {
    vi.stubGlobal('document', { documentElement: {} });
    vi.stubGlobal('getComputedStyle', () => ({
      getPropertyValue: () => '', // token missing at runtime
    }));
    expect(getColorRgb('teal')).toBe('0,191,166');
  });

  it('defaults an unknown color to the teal fallback rather than undefined', () => {
    // Defensive: a mis-typed color string must not yield `rgba(undefined,…)`.
    expect(getColorRgb('bogus' as BrandColor)).toBe('0,191,166');
  });
});

describe('getColorRgb — one home', () => {
  it('is defined ONLY in $lib/color-rgb.ts, nowhere else under launcher/src', () => {
    // This test file lives at launcher/src/lib/ — walk up to `src`.
    const here = dirname(fileURLToPath(import.meta.url));
    const srcRoot = resolve(here, '..');
    const canonical = resolve(here, 'color-rgb.ts');

    // Matches a `function getColorRgb` declaration or a `const getColorRgb =`
    // arrow assignment — the two ways a duplicate could reappear.
    const defPattern = /(?:function\s+getColorRgb\b|(?:const|let|var)\s+getColorRgb\s*=)/;

    const offenders: string[] = [];
    const walk = (dir: string): void => {
      // `withFileTypes` classifies each entry from the single readdir call,
      // so there is no separate stat() check-then-use (avoids the TOCTOU
      // pattern CodeQL flags as js/file-system-race).
      for (const dirent of readdirSync(dir, { withFileTypes: true })) {
        const entry = dirent.name;
        const full = join(dir, entry);
        if (dirent.isDirectory()) {
          if (entry === 'node_modules' || entry === '.svelte-kit' || entry === 'build') continue;
          walk(full);
          continue;
        }
        if (!/\.(svelte|ts|js)$/.test(entry)) continue;
        if (full === canonical) continue; // the one legitimate home
        if (full === fileURLToPath(import.meta.url)) continue; // this gate file
        if (defPattern.test(readFileSync(full, 'utf-8'))) {
          offenders.push(full);
        }
      }
    };
    walk(srcRoot);

    expect(
      offenders,
      `getColorRgb must be defined only in $lib/color-rgb.ts (duplicate ` +
        `definitions found in: ${offenders.join(', ')})`,
    ).toEqual([]);
  });
});
