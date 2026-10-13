// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.102 W6-AA: the extra code-graph paths panel is shown as "Add code
// folder" (owner naming 2026-10-13). Two pins:
//   1. the wording itself (owner's exact help line);
//   2. the panel RENDERS it — checked on the component's parsed template
//      (svelte/compiler `parse`), not on its source text, so a comment or a
//      dead string cannot satisfy the check: the heading, help line, button,
//      list label and empty state must be expression tags reading
//      CODE_FOLDER, and no template text may still say "Extra codegraph
//      paths" or "Add path".

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import {
  CODE_FOLDER,
  disabledTitle,
  reenabledTitle,
  removeConfirm,
  removedTitle,
} from './code-folder-labels';

type Node = { type?: string; [k: string]: unknown };

function walk(node: unknown, out: Node[]): void {
  if (Array.isArray(node)) {
    for (const n of node) walk(n, out);
    return;
  }
  if (!node || typeof node !== 'object') return;
  const n = node as Node;
  if (typeof n.type === 'string') out.push(n);
  for (const [k, v] of Object.entries(n)) {
    if (k === 'parent' || k === 'metadata') continue;
    walk(v, out);
  }
}

function memberText(expr: unknown): string {
  const e = expr as { type?: string; object?: { name?: string }; property?: { name?: string } };
  if (e?.type === 'MemberExpression' && e.object?.name && e.property?.name) {
    return `${e.object.name}.${e.property.name}`;
  }
  return '';
}

const panelSrc = readFileSync(
  fileURLToPath(new URL('./ExtraCodegraphPathsPanel.svelte', import.meta.url)),
  'utf8',
);
const ast = parse(panelSrc, { modern: true }) as unknown as { fragment: unknown };
const nodes: Node[] = [];
walk(ast.fragment, nodes);

function elementChildrenExpr(name: string): string[] {
  return nodes
    .filter((n) => n.type === 'RegularElement' && n.name === name)
    .flatMap((el) => {
      const kids: Node[] = [];
      walk((el as { fragment?: unknown }).fragment, kids);
      return kids.filter((k) => k.type === 'ExpressionTag').map((k) => memberText(k.expression));
    });
}

describe('code-folder labels (owner wording)', () => {
  it('names the option "Add code folder" with the owner help line', () => {
    expect(CODE_FOLDER.addButton).toBe('Add code folder');
    expect(CODE_FOLDER.help).toBe(
      "Indexed in this project's code graph, and subagents may get isolated worktrees there.",
    );
  });

  it('modal titles and confirm text speak of code folders', () => {
    expect(reenabledTitle()).toContain('code folder');
    expect(disabledTitle('P')).toBe('Re-syncing P codegraph after disabling code folder');
    expect(removedTitle('P')).toBe('Re-syncing P codegraph after code folder removal');
    expect(removeConfirm('/x')).toMatch(/^Remove code folder '\/x' from this project\?/);
  });
});

describe('ExtraCodegraphPathsPanel renders the code-folder labels', () => {
  it('heading, help line, button, empty state come from CODE_FOLDER', () => {
    expect(elementChildrenExpr('h4')).toContain('CODE_FOLDER.heading');
    expect(elementChildrenExpr('p')).toEqual(
      expect.arrayContaining(['CODE_FOLDER.help', 'CODE_FOLDER.details', 'CODE_FOLDER.empty']),
    );
    const buttonExprs = nodes
      .filter((n) => n.type === 'RegularElement' && n.name === 'button')
      .flatMap((el) => {
        const kids: Node[] = [];
        walk((el as { fragment?: unknown }).fragment, kids);
        return kids.map((k) => memberText(k));
      });
    expect(buttonExprs.join(' ')).toContain('CODE_FOLDER.addButton');
  });

  it('list and add-button aria labels come from CODE_FOLDER', () => {
    const attrExprs = nodes
      .filter((n) => n.type === 'Attribute' && n.name === 'aria-label')
      .flatMap((a) => {
        const kids: Node[] = [];
        walk((a as { value?: unknown }).value, kids);
        return kids.filter((k) => k.type === 'ExpressionTag').map((k) => memberText(k.expression));
      });
    expect(attrExprs).toEqual(
      expect.arrayContaining(['CODE_FOLDER.addAria', 'CODE_FOLDER.listAria', 'CODE_FOLDER.disabledAria']),
    );
  });

  it('no rendered template text keeps the old names', () => {
    const text = nodes
      .filter((n) => n.type === 'Text')
      .map((n) => String(n.data ?? ''))
      .join(' ');
    expect(text).not.toMatch(/Extra codegraph paths|Add path\b|extra paths added/);
  });
});
