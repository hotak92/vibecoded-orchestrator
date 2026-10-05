// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the Packs tab (v0.2.101 catalogue plan §3.6 / §9.4).
//
// THE DEFECT CLASS IT PINS: a Tauri command that exists on one side only.
// The invoke-name census (`invoke-names.test.ts`) already fails a literal
// invoke with no `generate_handler!` registration and a registration with
// no caller; this file adds the packs-specific locks the census cannot
// express:
//   * PacksTab invokes EXACTLY the two pack commands (AST — a grep is
//     satisfied by the comments that explain the wiring);
//   * both are registered in `generate_handler!`;
//   * the TS `PackInfo` mirror matches the Rust `PackInfo` struct
//     field-for-field (a drifted mirror type-checks and silently
//     `undefined`s a column at runtime).
//
// WHAT IT STILL DOES NOT PROVE: that a click reaches the handler in a real
// DOM, or that `vco_lib.packs status` answers (that CLI is lane L1's
// deliverable, pinned on the Python side by tests/test_packs_install.py).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { loadRust, sourceFile } from './test-support/source-census';
// SF-5 (v0.2.101): the shared extractors' ONE home — no local copies (an
// earlier copy of `registeredCommands` here is what the L3 review flagged).
import { registeredCommands, walk, isIdent } from './test-support/wiring-ast';

const here = dirname(fileURLToPath(import.meta.url));

// ─── Svelte AST helpers (walk / isIdent come from test-support/wiring-ast) ──

type Node = Record<string, unknown>;

/** The literal first arguments of every `invoke(...)` call in `src`. */
function invokeLiterals(root: unknown): string[] {
  return [...walk(root)]
    .filter((n) => n.type === 'CallExpression' && isIdent(n.callee, 'invoke'))
    .map((n) => {
      const a = (n.arguments as Node[])[0];
      return a?.type === 'Literal' && typeof a.value === 'string' ? a.value : `NON-LITERAL@${JSON.stringify(a ?? {})}`;
    });
}

const PACKS_TAB_SRC = readFileSync(resolve(here, 'project-state/PacksTab.svelte'), 'utf8');
const PACKS_TAB = parse(PACKS_TAB_SRC, { modern: true }) as unknown as Node;

const RUST = loadRust();
const LIB = RUST.find((f) => f.rel === 'src/lib.rs')!;
const PACKS_CMD = RUST.find((f) => f.rel === 'src/commands/packs_cmd.rs')!;

// ─── the packs wiring ──────────────────────────────────────────────────────

describe('PacksTab — invokes exactly the two pack commands', () => {
  it('every invoke in the component is a literal naming a pack command', () => {
    const literals = invokeLiterals((PACKS_TAB.instance as Node).content);
    expect(literals.length, 'PacksTab makes no invoke calls at all').toBeGreaterThan(0);
    expect(new Set(literals).size, 'duplicate invoke literals').toBe(literals.length);
    for (const name of literals) {
      expect(['list_project_packs', 'set_project_pack_enabled']).toContain(name);
    }
  });

  it('lists via list_project_packs and toggles via set_project_pack_enabled', () => {
    const literals = invokeLiterals((PACKS_TAB.instance as Node).content);
    expect(literals).toContain('list_project_packs');
    expect(literals).toContain('set_project_pack_enabled');
  });

  it('the toggle passes the pack name and enabled flag through', () => {
    const script = (PACKS_TAB.instance as Node).content;
    const toggle = [...walk(script)].find(
      (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, 'toggle'),
    );
    expect(toggle, 'no toggle function').toBeTruthy();
    const literals = invokeLiterals(toggle!);
    expect(literals).toEqual(['set_project_pack_enabled']);
    // The payload names the three parameters the Rust command takes.
    const src = PACKS_TAB_SRC;
    expect(src).toContain('projectId');
    expect(src).toContain('pack,');
    expect(src).toContain('enabled,');
  });
});

describe('generate_handler! registers both pack commands', () => {
  it('list_project_packs and set_project_pack_enabled are registered', () => {
    const registered = registeredCommands(LIB);
    expect(registered).toContain('list_project_packs');
    expect(registered).toContain('set_project_pack_enabled');
  });
});

// ─── the PackInfo mirror ───────────────────────────────────────────────────

/** Field names of `pub struct <name> { … }` in a Rust source text. */
function rustStructFields(src: string, struct: string): string[] {
  const m = new RegExp(`pub struct ${struct}\\s*\\{([^}]*)\\}`).exec(src);
  expect(m, `struct ${struct} not found`).toBeTruthy();
  return [...m![1].matchAll(/pub\s+([a-z_][a-z0-9_]*)\s*:/g)].map((x) => x[1]);
}

/** Field names of `export interface <name> { … }` in a TS source text. */
function tsInterfaceFields(src: string, iface: string): string[] {
  const m = new RegExp(`export interface ${iface}\\s*\\{([^}]*)\\}`).exec(src);
  expect(m, `interface ${iface} not found`).toBeTruthy();
  return [...m![1].matchAll(/^\s{2}([A-Za-z_][A-Za-z0-9_]*)\?*\s*:/gm)].map((x) => x[1]);
}

describe('TS PackInfo mirrors the Rust PackInfo struct', () => {
  it('field sets are identical', () => {
    const tsSrc = readFileSync(resolve(here, 'types/project-state.ts'), 'utf8');
    const rust = rustStructFields(PACKS_CMD.text, 'PackInfo');
    const ts = tsInterfaceFields(tsSrc, 'PackInfo');
    expect(rust.length).toBeGreaterThan(0);
    expect(ts.sort()).toEqual([...rust].sort());
  });
});

// ─── scanner self-check (red-proof scaffold) ───────────────────────────────

describe('scanner fixtures', () => {
  it('invokeLiterals sees calls, not comments or strings', () => {
    const f = sourceFile(
      'x.svelte',
      `<script>// invoke('in_comment')\nconst s = "invoke('in_string')";\ninvoke('real');\nsafeInvoke('soft');\n</script>`,
      'ts',
    );
    expect(invokeLiterals(f.text)).toEqual([]);
    const ast = parse(
      `<script>\n// invoke('in_comment')\ninvoke('real');\nawait invoke('typed');\n</script>`,
      { modern: true },
    ) as unknown as Node;
    expect(invokeLiterals((ast.instance as Node).content).sort()).toEqual(['real', 'typed']);
  });
});
