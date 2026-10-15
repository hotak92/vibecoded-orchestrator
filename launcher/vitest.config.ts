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
import { svelte } from '@sveltejs/vite-plugin-svelte';

const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  // v0.2.103 (review MEDIUM-2): compile `.svelte` components so the
  // Toast component test can mount the REAL renderer. This is the plain
  // vite-plugin-svelte compiler pass — NOT the sveltekit pipeline the
  // header note above rules out (no router/adapter is stood up);
  // pure-.ts tests are unaffected (the plugin only transforms .svelte
  // modules). Component tests opt into the DOM per file via the
  // `@vitest-environment jsdom` docblock (jsdom is a devDependency).
  plugins: [svelte()],
  resolve: {
    // Resolve svelte to its CLIENT entry (`browser` export condition) —
    // vitest's node runner otherwise resolves the server entry, where
    // `mount()` throws lifecycle_function_unavailable. Pure-.ts tests
    // are unaffected (none of their imports branch on this condition).
    conditions: ['browser'],
    alias: {
      // Mirror sveltekit's `$lib` → `src/lib` alias.
      $lib: resolve(here, 'src/lib'),
      // v0.2.93: `stores/ui.ts` (now imported by `stores/updater.ts` for
      // the progress-overlay open/close) imports `goto` from
      // `$app/navigation`, a SvelteKit virtual module that doesn't exist
      // outside the kit pipeline. Point it at an inert stub so store tests
      // can load the REAL ui store and assert on its flags.
      '$app/navigation': resolve(here, 'src/lib/test-stubs/app-navigation.ts'),
    },
  },
  test: {
    // Node stays the DEFAULT environment; the helpers under test are
    // dependency-free TS modules. Component tests that need a DOM opt
    // in per file with the `@vitest-environment jsdom` docblock (see
    // src/lib/components/Toast.test.ts; jsdom is a devDependency).
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
