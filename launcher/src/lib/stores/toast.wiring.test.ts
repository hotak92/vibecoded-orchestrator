// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the v0.2.103 toast dwell policy (field, 2026-10-14).
//
// THE DEFECT IT PINS: the owner clicked a code-graph update action; its
// outcome toast vanished before it could be read, so a walk that HAD
// started looked like a failure and was clicked again. The dwell policy
// now lives in ONE place (`stores/toast.ts::toastDwellMs`), so the thing
// to guard is the WIRING, in both directions:
//
//   (a) the code-graph update action's outcome goes THROUGH the toast
//       store (`toast.success` / `toast.error`, no bespoke timer) — every
//       `toast.*` call site inherits the policy for free, and a call site
//       growing its own `setTimeout` would silently leave it;
//   (b) the renderer (`Toast.svelte`) feeds the store the hover signal
//       (`toast.setHovered` on pointerenter/pointerleave) — without it
//       the pause half of the policy is dead code.
//
// AST-walked (not grepped) per the established pattern — comments satisfy
// a grep. See projects-page-affordances.wiring.test.ts.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { isIdent, walk } from '../test-support/wiring-ast';

const here = dirname(fileURLToPath(import.meta.url));

type Node = Record<string, any>;

function instanceScript(rel: string): Node {
  const src = readFileSync(resolve(here, rel), 'utf8');
  const parsed = parse(src, { modern: true }) as unknown as Node;
  return parsed.instance.content as Node;
}

/** Every `a.b(...)` / `a.b.c(...)` call name inside a subtree. */
function memberCalls(root: Node): string[] {
  const out: string[] = [];
  for (const n of walk(root)) {
    if (n.type !== 'CallExpression') continue;
    const callee = n.callee as Node | undefined;
    if (callee?.type === 'MemberExpression') {
      const obj = callee.object as Node | undefined;
      const prop = callee.property as Node | undefined;
      const objName =
        obj?.type === 'Identifier'
          ? obj.name
          : obj?.type === 'MemberExpression' && obj.object?.type === 'Identifier'
            ? obj.object.name
            : '';
      if (objName && prop?.type === 'Identifier') out.push(`${objName}.${prop.name}`);
    }
  }
  return out;
}

/** Bare identifier calls (`invoke(...)`, `setTimeout(...)`) in a subtree. */
function bareCalls(root: Node): string[] {
  const out: string[] = [];
  for (const n of walk(root)) {
    if (n.type !== 'CallExpression') continue;
    const callee = n.callee as Node | undefined;
    if (callee?.type === 'Identifier') out.push(callee.name);
  }
  return out;
}

describe('code-graph update action outcome uses the ONE toast policy', () => {
  // The project page's "Re-build code graph" button — the code-graph
  // update action whose outcome toast triggered the field report.
  const page = instanceScript('../../routes/project/[id]/+page.svelte');
  const fn = [...walk(page)].find(
    (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, 'rebuildCodeGraph'),
  );
  const calls = fn ? memberCalls(fn) : [];
  const bare = fn ? bareCalls(fn) : [];

  it('routes its outcome through the toast store', () => {
    expect(fn, 'rebuildCodeGraph exists on the project page').toBeTruthy();
    expect(bare).toContain('invoke');
    expect(calls).toContain('toast.success');
    expect(calls).toContain('toast.error');
  });

  it('keeps no bespoke auto-dismiss timer of its own', () => {
    expect(bare).not.toContain('setTimeout');
  });
});

describe('the renderer feeds the store the hover signal', () => {
  const comp = readFileSync(
    resolve(here, '../components/Toast.svelte'),
    'utf8',
  );
  const parsed = parse(comp, { modern: true }) as unknown as Node;

  it('wires pointerenter/pointerleave to toast.setHovered', () => {
    const calls = memberCalls(parsed.fragment as Node);
    expect(calls).toContain('toast.setHovered');
    expect(comp).toContain('onpointerenter');
    expect(comp).toContain('onpointerleave');
  });

  it('still wires manual dismiss through the store', () => {
    const calls = memberCalls(parsed.fragment as Node);
    expect(calls).toContain('toast.dismiss');
  });
});
