// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// SF-1 (v0.2.101 re-review): a legacy diagram-wrapper row (appended by the
// Rust `with_legacy_wrapper_rows` with `configurable: false`) must NOT get
// the destructive GLOBAL toggle switch — the switch's OFF deregisters the
// entry from ~/.claude.json (a one-way loss: ON is refused because the
// registration builder retired the wrappers). The non-destructive channel
// for these rows is the per-project Permissions toggle
// (`disabledMcpServers`).
//
// McpDashboard.svelte cannot be SSR-rendered here (it imports `$lib` and
// invokes Tauri at mount), so this pins the TEMPLATE structure via the
// Svelte AST — the same approach the RlScoringSwitch wiring test above it
// in the suite uses.

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';

const SRC = readFileSync(
  fileURLToPath(new URL('./components/McpDashboard.svelte', import.meta.url)),
  'utf8',
);
const AST = parse(SRC, { modern: true });

type Node = Record<string, any>;

/** Depth-first collect every object in the AST matching `pred`. */
function collect(node: unknown, pred: (n: Node) => boolean, out: Node[] = []): Node[] {
  if (!node || typeof node !== 'object') return out;
  if (Array.isArray(node)) {
    for (const item of node) collect(item, pred, out);
    return out;
  }
  const n = node as Node;
  if (pred(n)) out.push(n);
  for (const v of Object.values(n)) collect(v, pred, out);
  return out;
}

function attrSource(attr: Node): string {
  return SRC.slice(attr.start, attr.end);
}

function inputAttrs(el: Node): string[] {
  return (el.attributes as Node[]).map((a) => a.name);
}

describe('McpDashboard global switch for configurable:false rows (SF-1)', () => {
  // The `{#if server.configurable}` gate around the toggle (a bare
  // MemberExpression — distinct from the `server.configurable && ...` gate
  // on the settings panel below it).
  const gate = collect(AST.fragment, (n) => n.type === 'IfBlock').find((n) =>
    SRC.slice(n.test.start, n.test.end) === 'server.configurable',
  );
  // Modern Svelte AST: `alternate` is an ElseBlock whose payload is `nodes`.
  const elseFragment = gate?.alternate?.nodes ?? [];

  function branchInputs(fragment: unknown): Node[] {
    // Svelte 5 modern AST names template elements `RegularElement`.
    return collect(fragment, (n) => /Element$/.test(String(n.type)) && n.name === 'input');
  }

  it('the global toggle switch is gated on server.configurable', () => {
    expect(gate, 'expected an {#if server.configurable} block around the toggle').toBeTruthy();
    expect(gate!.alternate, 'expected an {:else} branch for the legacy row').toBeTruthy();
  });

  it('configurable rows: switch is live (has onchange, not disabled)', () => {
    const inputs = branchInputs(gate!.consequent);
    expect(inputs.length).toBeGreaterThan(0);
    const attrs = inputs.map(inputAttrs);
    for (const a of attrs) {
      expect(a).toContain('onchange');
      expect(a).not.toContain('disabled');
    }
    // The live switch calls the toggle command.
    expect(
      collect(gate!.consequent, (n) => n.type === 'Attribute' && n.name === 'onchange').some(
        (a) => /toggleMcp\(server\.id/.test(attrSource(a)),
      ),
    ).toBe(true);
  });

  it('configurable:false (legacy wrapper) rows: switch is inert — disabled, no onchange — and the note names the per-project Permissions channel', () => {
    const inputs = branchInputs(elseFragment);
    expect(inputs.length).toBeGreaterThan(0);
    for (const input of inputs) {
      const attrs = inputAttrs(input);
      expect(attrs).toContain('disabled');
      expect(attrs).not.toContain('onchange');
      const testid = (input.attributes as Node[]).find((a) => a.name === 'data-testid');
      expect(testid, 'legacy switch carries a stable data-testid').toBeTruthy();
      expect(attrSource(testid!)).toMatch(/mcp-legacy-switch-/);
    }
    const text = collect(elseFragment, (n) => n.type === 'Text')
      .map((n) => n.data as string)
      .join(' ');
    expect(text).toMatch(/Per-project toggle only/);
    expect(text).toMatch(/Permissions/);
  });
});
