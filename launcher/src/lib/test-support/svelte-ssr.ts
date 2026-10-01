// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.100 (W5R-09): render a SELF-CONTAINED Svelte component to HTML in the
// node vitest environment, so a test can assert on the DOM state it produces
// (e.g. `checked` / `disabled`) instead of on its source text.
//
// The vitest config deliberately runs without the SvelteKit/Svelte Vite
// plugin and without a DOM, so the component is compiled here with
// `svelte/compiler` (server output), its `svelte/*` imports are pinned to the
// installed package files, and the result is rendered with `svelte/server`.
//
// Limitation (by design): only `svelte/*` imports are resolved. A component
// that imports `$lib/...` at runtime (type-only imports are erased) cannot be
// rendered here — keep the component under test presentational.

import { compile } from 'svelte/compiler';
import { render } from 'svelte/server';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const requireHere = createRequire(import.meta.url);

export async function renderSvelte(
  componentPath: string,
  props: Record<string, unknown>,
): Promise<string> {
  const src = readFileSync(componentPath, 'utf8');
  const out = compile(src, { generate: 'server', filename: componentPath });
  const code = out.js.code.replace(/from\s+(['"])(svelte(?:\/[^'"]*)?)\1/g, (_m, _q, spec: string) => {
    return `from '${pathToFileURL(requireHere.resolve(spec)).href}'`;
  });
  const dir = mkdtempSync(join(tmpdir(), 'vco-svelte-ssr-'));
  const file = join(dir, 'component.mjs');
  try {
    writeFileSync(file, code, 'utf8');
    const mod = await import(/* @vite-ignore */ pathToFileURL(file).href);
    return render(mod.default, { props }).body;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
