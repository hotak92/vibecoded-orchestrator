// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the module-deprecation banner (v0.2.31 Layer 1; pinned
// by W5-B S3 in v0.2.102). The Rust half — that the L0 catalog's
// `deprecated` flag reaches the catalog entry — is red-proofed in
// `commands::modules::tests::list_module_catalog_carries_l0_deprecation_
// to_the_entry`. This file pins the FE half: ModuleCatalog derives the
// deprecated-AND-installed set and renders one DeprecationBanner per
// entry, and the banner component itself is visible exactly when the
// entry says deprecated (and not dismissed).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from '../test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
function load(rel: string): Node {
  return parse(readFileSync(resolve(here, rel), 'utf8'), {
    modern: true,
  }) as unknown as Node;
}
const CATALOG = load('ModuleCatalog.svelte');
const BANNER = load('DeprecationBanner.svelte');

describe('ModuleCatalog renders the deprecation banner for deprecated INSTALLED modules (S3)', () => {
  it('parses', () => {
    expect([...walk(CATALOG)].length).toBeGreaterThan(100);
  });

  it('derives deprecatedInstalled from the entry deprecated flag AND installed state', () => {
    const decl = [...walk((CATALOG.instance as Node).content)].find(
      (n) =>
        n.type === 'VariableDeclaration' &&
        (n.declarations as Node[]).some((d) => isIdent(d.id, 'deprecatedInstalled')),
    );
    expect(decl, 'no deprecatedInstalled derivation').toBeTruthy();
    const nodes = [...walk(decl!)];
    // Reads m.deprecated…
    expect(
      nodes.some((n) => n.type === 'MemberExpression' && isIdent((n as Node).property, 'deprecated')),
      'the derivation never reads the deprecated field',
    ).toBe(true);
    // …AND the installed gate (installed.has or kind === "installed").
    expect(
      nodes.some((n) => n.type === 'MemberExpression' && isIdent((n as Node).object, 'installed'))
        || nodes.some((n) => JSON.stringify(n).includes('"installed"')),
      'the derivation has no installed gate',
    ).toBe(true);
  });

  it('renders one DeprecationBanner per deprecatedInstalled entry', () => {
    const each = [...walk(CATALOG.fragment)].filter(
      (n) => n.type === 'EachBlock' && JSON.stringify(n.expression).includes('deprecatedInstalled'),
    );
    expect(each.length, 'no {#each deprecatedInstalled} block').toBe(1);
    expect(
      [...walk(each[0])].some((n) => n.type === 'Component' && (n as Node).name === 'DeprecationBanner'),
      'the each-block does not render DeprecationBanner',
    ).toBe(true);
  });
});

describe('DeprecationBanner is visible exactly when the entry is deprecated (S3)', () => {
  it('derives visibility from the deprecated flag (both arms)', () => {
    const decl = [...walk((BANNER.instance as Node).content)].find(
      (n) =>
        n.type === 'VariableDeclaration' &&
        (n.declarations as Node[]).some((d) => isIdent(d.id, 'visible')),
    );
    expect(decl, 'no visible derivation').toBeTruthy();
    const text = JSON.stringify(decl!);
    expect(text).toContain('deprecated');
    expect(text).toContain('dismissed');
    // The negation arm: an undeprecated module renders NO banner.
    expect(text).toContain('!');
  });
});
