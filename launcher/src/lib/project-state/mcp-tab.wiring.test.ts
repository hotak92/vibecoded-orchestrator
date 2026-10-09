// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the per-project MCP registry tab (v0.2.102, W5-B C4).
//
// THE DEFECT IT PINS: `set_project_mcp_server_enabled` and
// `unregister_project_mcp_server` were registered Tauri commands with NO
// caller anywhere under launcher/src — the migration-010 registry surface
// existed in the DB and the command layer, and no screen reached it. The
// tab is the reachable component; this file pins that it stays wired.
//
// Parsed with the Svelte compiler so a mention in a comment or a string
// cannot satisfy it (same shape as secret-ref-status.wiring.test.ts).
// The ~/.claude.json mirror itself is pinned on the RUST side
// (set_project_mcp_server_enabled_flips_row_and_mirrors_claude_json).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from '../test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const TAB = parse(
  readFileSync(resolve(here, 'McpTab.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;
const PAGE = parse(
  readFileSync(resolve(here, '../../routes/project/[id]/+page.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;

/** Literal-argument invoke sites (`invoke('name', …)`) in a subtree. */
function literalInvokes(root: unknown, name: string): Node[] {
  return [...walk(root)].filter((n) => {
    if (n.type !== 'CallExpression') return false;
    const callee = n.callee as Node | undefined;
    if (!isIdent(callee, 'invoke')) return false;
    const arg = (n.arguments as Node[] | undefined)?.[0];
    return arg?.type === 'Literal' && arg.value === name;
  });
}

/** The subtree of a named function declaration in the instance script. */
function fnBody(ast: Node, name: string): Node {
  const fn = [...walk((ast.instance as Node).content)].find(
    (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, name),
  );
  expect(fn, `no function declaration named ${name}`).toBeTruthy();
  return fn!;
}

describe('McpTab is wired to the migration-010 registry commands (W5-B C4)', () => {
  it('parses (guards against vacuously-passing assertions)', () => {
    expect([...walk(TAB)].length).toBeGreaterThan(100);
  });

  it('loads its rows through list_project_mcp_servers', () => {
    expect(literalInvokes(fnBody(TAB, 'load'), 'list_project_mcp_servers').length).toBe(1);
  });

  it('toggles through set_project_mcp_server_enabled — not a second .claude.json writer', () => {
    // Scoped to toggleMcp's own subtree: the call must sit in the handler
    // the checkbox onchange actually reaches, not in a dead sibling.
    const calls = literalInvokes(fnBody(TAB, 'toggleMcp'), 'set_project_mcp_server_enabled');
    expect(calls.length).toBe(1);
    // The toggle passes the NEXT state (row.enabled was already flipped
    // optimistically) — an invoke that re-sends the pre-toggle value would
    // no-op the registry row while still writing the mirror.
    const args = calls[0].arguments as Node[];
    expect(
      [...walk(args)].some((n) => isIdent(n, 'next')),
      'the toggle must send the NEXT state, not the pre-toggle value',
    ).toBe(true);
  });

  it('unregisters through unregister_project_mcp_server, behind a confirm guard (cancel arm)', () => {
    const fn = fnBody(TAB, 'unregisterMcp');
    // The cancel arm: confirm() must be consulted BEFORE the invoke, and a
    // refusal returns without invoking. (Order pinned by index comparison —
    // an invoke that precedes the guard is the act-without-consent defect.)
    const nodes = [...walk(fn)];
    const confirmAt = nodes.findIndex(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, 'confirm'),
    );
    const invokeAt = nodes.findIndex((n) =>
      n.type === 'CallExpression' &&
      isIdent(n.callee, 'invoke') &&
      (n.arguments as Node[])[0]?.type === 'Literal' &&
      ((n.arguments as Node[])[0] as Node).value === 'unregister_project_mcp_server',
    );
    expect(confirmAt, 'unregisterMcp never calls confirm()').toBeGreaterThanOrEqual(0);
    expect(invokeAt, 'unregisterMcp never invokes unregister_project_mcp_server').toBeGreaterThan(
      confirmAt,
    );
    // The early return on refusal (`if (!sure) return;`).
    const returns = nodes.filter((n) => n.type === 'ReturnStatement');
    expect(returns.length, 'no cancel-arm early return').toBeGreaterThan(0);
  });

  it('renders the Unregister action only for user-added rows', () => {
    // The button must sit inside an {#if row.is_user_added} guard — a
    // row-classification the backend computes, not a client-side guess.
    // (Red-proof mutation: drop the guard and this fails.)
    const guarded = [...walk(TAB.fragment)].filter((n) => {
      if (n.type !== 'IfBlock') return false;
      const t = n.test as Node | undefined;
      return (
        t?.type === 'MemberExpression' && isIdent(t.object, 'row') && isIdent(t.property, 'is_user_added')
      );
    });
    expect(guarded.length, 'no {#if row.is_user_added} block').toBe(1);
    expect(
      [...walk(guarded[0])].some(
        (n) =>
          n.type === 'RegularElement' &&
          n.name === 'button' &&
          [...walk(n)].some(
            (x) =>
              x.type === 'CallExpression' &&
              isIdent((x as Node).callee, 'unregisterMcp'),
          ),
      ),
      'the user-added guard does not contain the Unregister button',
    ).toBe(true);
  });

  it('is mounted by the project page (reachable component)', () => {
    const mounted = [...walk(PAGE.fragment)].some(
      (n) => n.type === 'Component' && (n as Node).name === 'McpTab',
    );
    expect(mounted, 'the project page never mounts McpTab').toBe(true);
    // And the tab strip offers it.
    const source = readFileSync(resolve(here, '../../routes/project/[id]/+page.svelte'), 'utf8');
    expect(source).toContain("{ id: 'mcp', label: 'MCP servers' }");
  });
});
