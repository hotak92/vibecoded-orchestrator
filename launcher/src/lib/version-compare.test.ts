// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 WP-01: the TS version home, driven by the ONE case table shared
// with vco_lib/version_compare.py and vct-launcher-core/src/version.rs.

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { VersionParseError, compareVersions, parseVersion, semverLess } from './version-compare';

interface Cases {
  order: { a: string; b: string; cmp: -1 | 0 | 1 }[];
  reject: string[];
}

const here = dirname(fileURLToPath(import.meta.url));
const cases: Cases = JSON.parse(
  readFileSync(resolve(here, '../../../tests/fixtures/version_order_cases.json'), 'utf8'),
);

describe('version-compare — shared case table', () => {
  it('the table is not empty', () => {
    expect(cases.order.length).toBeGreaterThanOrEqual(6);
    expect(cases.reject.length).toBeGreaterThanOrEqual(6);
  });

  it.each(cases.order)('order: $a vs $b → $cmp', ({ a, b, cmp }) => {
    expect(compareVersions(a, b)).toBe(cmp);
    expect(compareVersions(b, a)).toBe(-cmp || 0);
    expect(semverLess(a, b)).toBe(cmp < 0);
  });

  it.each(cases.reject.map((r) => [r]))('reject: %j', (bad) => {
    let caught: unknown = null;
    try {
      parseVersion(bad);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(VersionParseError);
    expect((caught as VersionParseError).text).toBe(bad);
    expect(() => semverLess(bad, '0.2.100')).toThrow(VersionParseError);
    expect(() => semverLess('0.2.100', bad)).toThrow(VersionParseError);
    expect(() => compareVersions(bad, '0.2.100')).toThrow(VersionParseError);
  });
});
