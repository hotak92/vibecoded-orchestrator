// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

import { describe, it, expect } from 'vitest';
import { addEnvNote, normalizeEnvKey, type EnvNote } from './env-notes';

function rows(...keys: string[]): EnvNote[] {
  return keys.map((key) => ({ key, value: '' }));
}

describe('env notes (review R8 M1)', () => {
  it('normalizes the key: trimmed, uppercased', () => {
    expect(normalizeEnvKey('  my_var ')).toBe('MY_VAR');
  });

  it('appends a new key', () => {
    const res = addEnvNote(rows('A'), '  b ', 'note');
    expect(res.refused).toBeNull();
    expect(res.entries.map((e) => e.key)).toEqual(['A', 'B']);
    expect(res.entries[1]!.value).toBe('note');
  });

  it('REFUSES a duplicate key and keeps the list unchanged', () => {
    const before = rows('FOO');
    const res = addEnvNote(before, 'foo ', 'again');
    expect(res.refused).toBeTruthy();
    expect(res.refused).toContain('FOO');
    expect(res.entries).toBe(before); // unchanged, not a copy
  });

  it('an empty key is the caller no-op: unchanged list, no refusal', () => {
    const before = rows('A');
    const res = addEnvNote(before, '   ', 'x');
    expect(res.refused).toBeNull();
    expect(res.entries).toBe(before);
  });

  it('keys stay unique under any sequence of accepted adds (the each key can never collide)', () => {
    let entries: EnvNote[] = [];
    for (const raw of ['a', 'A', ' b', 'B ', 'a ', 'c']) {
      const res = addEnvNote(entries, raw, '');
      if (!res.refused) entries = res.entries;
    }
    expect(entries.map((e) => e.key).sort()).toEqual(['A', 'B', 'C']);
    expect(new Set(entries.map((e) => e.key)).size).toBe(entries.length);
  });
});
