// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.35 (Agent J): minimal vitest config for unit tests of pure
// `.ts` helpers under `src/lib/`. We deliberately do NOT load the
// sveltekit Vite plugin here — that would require @sveltejs/kit to
// stand up its full dev pipeline (router, adapters, etc.) just to run
// a handful of pure-function tests. Instead we re-implement just the
// `$lib` alias resolution so test files can import from `$lib/...`
// the same way the runtime code does.
//
// Test files live next to the source they exercise, suffix
// `.test.ts`. The pattern matches vitest's default discovery so no
// extra config is needed there.

import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      // Mirror sveltekit's `$lib` → `src/lib` alias.
      $lib: resolve(here, 'src/lib'),
    },
  },
  test: {
    // Pure node environment is fine; the helpers under test are
    // dependency-free TS modules. If we later add component tests
    // that need a DOM, we'd switch this to 'jsdom' (and add
    // @testing-library/svelte + jsdom as dev deps).
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
