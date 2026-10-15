// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.103 (review R8 B1): no component-local class name may collide with
// a Tailwind utility generated from the app.css theme tokens.
//
// THE DEFECT IT PINS (owner report): the status-banner shell used
// `class="bg-text"` for its title/body column. Tailwind v4 turns every
// `--color-*` theme token into a `bg-<token>` utility and emits it for
// any matching class it finds in the source, so that div got
// `background-color: var(--color-text)` (#F1F5F9 — a white box) behind
// light text; the owner saw the "Code graph: build failed" banner as an
// unreadable white box. The shell's own scoped `.bg-text` rule sets no
// background, so nothing overrode the utility.
//
// HOW IT WORKS (no DOM needed): compile the REAL app.css with the
// installed tailwindcss, build CSS for every class token that appears in
// a `class="…"` attribute or `class:` directive under src/**/*.svelte,
// then fail if any token Tailwind turned into a utility is not on the
// deliberate-use allow-list below.

import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { compile } from 'tailwindcss';
import { parse } from 'svelte/compiler';
import { walk } from './test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, '..');
const ROOT = resolve(SRC, '..');

/** Classes that INTENTIONALLY use a Tailwind utility. Everything else
 * must not produce one — a component-local name that Tailwind also
 * resolves silently changes the element's appearance (the B1 defect). */
const DELIBERATE_UTILITIES = new Set([
  // `sr-only` is used on purpose as the accessible-visually-hidden
  // utility (same effect as a hand-written rule, kept as Tailwind's).
  'sr-only',
]);

/** Every .svelte file under src, recursively. */
function svelteFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) out.push(...svelteFiles(p));
    else if (name.endsWith('.svelte')) out.push(p);
  }
  return out;
}

/** class tokens: `class="a b"` attribute text parts + `class:name`
 * directive names, with the file they came from. */
function classTokens(): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>();
  const add = (token: string, file: string) => {
    if (token === '') return;
    if (!map.has(token)) map.set(token, new Set());
    map.get(token)!.add(file);
  };
  for (const file of svelteFiles(SRC)) {
    const rel = file.slice(SRC.length + 1);
    let ast: Node;
    try {
      ast = parse(readFileSync(file, 'utf8'), { modern: true }) as unknown as Node;
    } catch {
      continue;
    }
    for (const n of walk(ast)) {
      if (n.type === 'Attribute' && n.name === 'class') {
        const v = n.value;
        const parts = Array.isArray(v) ? v : [v];
        for (const p of parts) {
          if (p && (p as Node).type === 'Text') {
            for (const t of String((p as Node).data ?? '').split(/\s+/)) {
              add(t, rel);
            }
          }
        }
      } else if (n.type === 'ClassDirective') {
        add(String(n.name ?? ''), rel);
      }
    }
  }
  return map;
}

/** Class names that appear as selectors inside `@layer utilities` of the
 * built CSS — i.e. the classes Tailwind GENERATED a rule for. */
function generatedUtilityClasses(css: string): Set<string> {
  const marker = '@layer utilities';
  const i = css.indexOf(marker);
  if (i < 0) return new Set();
  const open = css.indexOf('{', i);
  let depth = 0;
  let end = open;
  for (let j = open; j < css.length; j++) {
    if (css[j] === '{') depth++;
    else if (css[j] === '}') {
      depth--;
      if (depth === 0) {
        end = j;
        break;
      }
    }
  }
  const body = css.slice(open + 1, end);
  const out = new Set<string>();
  for (const m of body.matchAll(/\.(-?[A-Za-z_][A-Za-z0-9_-]*)/g)) {
    out.add(m[1]!);
  }
  return out;
}

describe('app.css utility collisions (review R8 B1)', () => {
  it('no component-local class token triggers a Tailwind utility', async () => {
    const tokens = classTokens();
    expect(tokens.size).toBeGreaterThan(500); // the scan actually scanned

    // compile() takes a CSS STRING, so it cannot resolve
    // `@import "tailwindcss"` on its own — hand it a resolver that
    // resolves bare package ids and relative paths against `from`.
    const req = createRequire(import.meta.url);
    const compiler = await compile(readFileSync(resolve(SRC, 'app.css'), 'utf8'), {
      base: ROOT,
      loadStylesheet: async (id: string, from?: string) => {
        const baseDir = from ? dirname(from) : SRC;
        // `@import "tailwindcss"` names the package's CSS entry, not its
        // JS main — resolve the exported stylesheet explicitly.
        const spec = id === 'tailwindcss' ? 'tailwindcss/index.css' : id;
        const resolved = spec.startsWith('.') || isAbsolute(spec)
          ? resolve(baseDir, spec)
          : req.resolve(spec);
        return {
          content: readFileSync(resolved, 'utf8'),
          base: dirname(resolved),
          path: resolved,
        };
      },
    });
    const built = compiler.build([...tokens.keys()]);

    const utilClasses = generatedUtilityClasses(built);
    const collisions = [...tokens.keys()]
      .filter((t) => utilClasses.has(t) && !DELIBERATE_UTILITIES.has(t))
      .sort();

    const detail = collisions.map(
      (t) =>
        `  .${t}  <- ${[...(tokens.get(t) ?? [])].sort().join(', ')}`,
    );
    expect(
      collisions,
      'component-local class names that Tailwind turned into utilities ' +
        '(rename them, or add them to DELIBERATE_UTILITIES with a reason ' +
        'if the utility is intended):\n' +
        detail.join('\n') +
        '\n',
    ).toEqual([]);
  });
});
