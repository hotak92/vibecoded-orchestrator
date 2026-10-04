// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the Services page's routing-guidance tri-state
// (v0.2.101, GUI gap G1).
//
// THE DEFECT IT PINS: the per-project "Model-routing guidance in project
// CLAUDE.md" list seeded its checkboxes from `is_project_module_active`
// alone (via `projectHasRoutingGuidance`). That reads only the explicit
// `project_modules` row, so every project WITHOUT a row showed — and the
// page copy said — "off", while the CLAUDE.md render (which falls back to
// the machine signal when no row exists) drew the section on every
// gateway-configured machine. A two-state toggle presented a tri-state.
//
// WHAT IS TESTED WHERE: the verdict mapping and the caption wording are
// unit-tested in `api/model_gateway.test.ts` (pure, `$lib/tauri` mocked) and
// on the Rust side (`commands/model_gateway.rs` — fixture payloads). This
// file pins the half those cannot see: that the page seeds its list from the
// new `routingGuidance` command (by FOLDER, the argument the gate keys on)
// and renders the "follows this machine" caption. Parsed with the Svelte
// compiler so a mention in a comment does not satisfy it (same shape as
// `bundle-staleness.wiring.test.ts`). It does not prove a real DOM paints
// the caption — `vitest.config.ts` has no component runner.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const PAGE = parse(
  readFileSync(resolve(here, '../routes/services/+page.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;

function* walk(node: unknown): Generator<Node> {
  if (node === null || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  const obj = node as Node;
  if (typeof obj.type === 'string') yield obj;
  for (const [k, v] of Object.entries(obj)) {
    if (k === 'parent' || k === 'loc') continue;
    yield* walk(v);
  }
}

function isIdent(n: unknown, name: string): boolean {
  return (n as Node | undefined)?.type === 'Identifier' && (n as Node).name === name;
}

/** `obj.prop` or `obj?.prop` (an optional member is wrapped in a ChainExpression). */
function isMember(n: unknown, obj: string, prop: string): boolean {
  let node = n as Node | undefined;
  if (node?.type === 'ChainExpression') node = node.expression as Node;
  return (
    node?.type === 'MemberExpression' && isIdent(node.object, obj) && isIdent(node.property, prop)
  );
}

function calls(root: unknown, name: string): Node[] {
  return [...walk(root)].filter(
    (n) => n.type === 'CallExpression' && isIdent(n.callee, name),
  );
}

describe('Services page — routing guidance is seeded from the render gate', () => {
  it('loads each project’s state with routingGuidance(p.folder_path)', () => {
    const loads = calls(PAGE, 'routingGuidance');
    expect(loads.length).toBeGreaterThan(0);
    // The seed loop asks per project, keyed by the FOLDER (the argument the
    // Python gate resolves); the post-toggle re-read passes the same value
    // through a local, which is why "at least one" is the assertion.
    const byFolder = loads.filter((c) =>
      isMember((c.arguments as Node[])[0], 'p', 'folder_path'),
    );
    expect(byFolder.length).toBeGreaterThan(0);
  });

  it('no longer reads the row-only projectHasRoutingGuidance (the G1 defect)', () => {
    expect(calls(PAGE, 'projectHasRoutingGuidance')).toHaveLength(0);
  });

  it('offers the way back: clearGuidance → clearProjectRoutingGuidance, only for explicit rows', () => {
    // The handler asks the generic module clear...
    expect(calls(PAGE, 'clearProjectRoutingGuidance').length).toBeGreaterThan(0);
    const source = readFileSync(
      resolve(here, '../routes/services/+page.svelte'),
      'utf8',
    );
    // ...from a "Follow this machine" control that the template gates on an
    // EXPLICIT choice existing (on or off) — never shown when the project
    // already follows the machine or the gate could not be asked.
    expect(source).toContain('Follow this machine');
    expect(source).toMatch(/g\.mode === 'on' \|\| g\.mode === 'off'|\{:else if g\}/);
    // And the "explicit choice sticks" caveat is gone — it described the
    // missing command this control now is.
    expect(source).not.toContain('does not yet expose a way');
  });

  it('renders the follows-the-machine caption through describeRoutingGuidance', () => {
    expect(calls(PAGE, 'describeRoutingGuidance').length).toBeGreaterThan(0);
    // And the checkbox itself is a tri-state: an `indeterminate` attribute
    // exists on an input whose value mentions 'follows_machine'.
    const attrs = [...walk(PAGE)].filter((n) => n.type === 'Attribute');
    const name = (a: Node) =>
      typeof a.name === 'string' ? a.name : (a.name as Node | undefined)?.name;
    const indeterminate = attrs.some((a) => name(a) === 'indeterminate');
    expect(indeterminate).toBe(true);
    const source = readFileSync(
      resolve(here, '../routes/services/+page.svelte'),
      'utf8',
    );
    expect(source).toContain("g?.mode === 'follows_machine'");
  });
});
