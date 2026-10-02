// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The ONE TypeScript home for parsing and ordering orchestrator/module
// versions (v0.2.100 WP-01).
//
// Rule (owner ruling Q7): a version is exactly three numeric parts —
// /^v?(\d+)\.(\d+)\.(\d+)$/ over ASCII digits. No pre-release suffix, no
// fourth number, no surrounding whitespace. Anything else throws
// `VersionParseError` carrying the offending string; it is never ranked.
//
// Sibling homes answer from the SAME case table,
// `tests/fixtures/version_order_cases.json`: `vco_lib/version_compare.py`
// (Python) and `vct-launcher-core/src/version.rs` (Rust).
//
// Superseded (recorded): `module-status-display.ts::semverLess` used to take
// each part's LEADING integer, so "0.2.4-dev" compared equal to "0.2.4" and
// "1.0" to "1.0.0". It lived there "duplicated … so the helper is
// self-contained for unit-testing"; it now lives here once and the display
// helper maps a parse error to "version unreadable: <s>".

export class VersionParseError extends Error {
  /** The offending value exactly as received. */
  readonly text: string;

  constructor(text: string) {
    super(`version ${JSON.stringify(text)} is not X.Y.Z (three numeric parts, no suffix)`);
    this.name = 'VersionParseError';
    this.text = text;
  }
}

// JS `\d` is ASCII-only (no `u` flag), matching the Rust/Python homes.
const VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)$/;

/** `"0.2.100"` / `"v0.2.100"` → `[0, 2, 100]`; anything else throws. */
export function parseVersion(text: string): [number, number, number] {
  // `$` without the `m` flag does not match before a trailing newline in JS.
  const m = typeof text === 'string' ? VERSION_RE.exec(text) : null;
  if (!m) throw new VersionParseError(String(text));
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

/** -1 / 0 / 1; throws `VersionParseError` when either side is unparseable. */
export function compareVersions(a: string, b: string): -1 | 0 | 1 {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  for (let i = 0; i < 3; i++) {
    if (pa[i] < pb[i]) return -1;
    if (pa[i] > pb[i]) return 1;
  }
  return 0;
}

/** `a < b`; throws `VersionParseError` when either side is unparseable. */
export function semverLess(a: string, b: string): boolean {
  return compareVersions(a, b) < 0;
}
