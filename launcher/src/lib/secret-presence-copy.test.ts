// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The coordination page rendered "Required." whenever the keychain read
// FAILED, because `coordination_get_config` returned a boolean and the
// error arm collapsed to `false`. A user who sees "Required." pastes the
// secret again — overwriting a keychain entry that was fine. These pin the
// tri-state that replaced it.

import { describe, it, expect } from 'vitest';
import { keyPlaceholder, keyHint } from './secret-presence-copy';

describe('keyPlaceholder / keyHint — unknown is never absence', () => {
  it('says a value is stored when the keychain answered "present"', () => {
    expect(keyPlaceholder('present', 'paste service key')).toContain('already set');
    expect(keyHint('present', 'Required.')).toContain('Leave blank to keep');
  });

  it('uses the caller\'s own wording for a genuine absence', () => {
    expect(keyPlaceholder('absent', 'paste service key')).toBe('paste service key');
    expect(keyPlaceholder('absent', 'optional')).toBe('optional');
    expect(keyHint('absent', 'Required.')).toBe('Required.');
  });

  it('NEVER renders an unreadable store as the empty case', () => {
    // The whole defect in one assertion.
    expect(keyPlaceholder('unknown', 'paste service key')).not.toBe('paste service key');
    expect(keyHint('unknown', 'Required.')).not.toBe('Required.');
  });

  it('tells the user unknown is not "not set", and warns off re-entry', () => {
    const hint = keyHint('unknown', 'Required.');
    expect(hint.toLowerCase()).toContain('not the same as');
    expect(hint.toLowerCase()).toContain('overwrite');
  });

  it('treats a not-yet-loaded config as unknown, not as absent', () => {
    // `config?.supabase_key_presence` is `undefined` on first paint. That
    // is "we have not looked", which is the unknown case.
    expect(keyPlaceholder(undefined, 'paste service key')).toBe(
      keyPlaceholder('unknown', 'paste service key'),
    );
    expect(keyHint(undefined, 'Required.')).toBe(keyHint('unknown', 'Required.'));
  });

  it('never emits a value or anything value-shaped', () => {
    const all = (['present', 'absent', 'unknown', undefined] as const)
      .map((p) => keyPlaceholder(p, 'x') + ' ' + keyHint(p, 'y'))
      .join(' ');
    expect(all).not.toMatch(/[A-Za-z0-9_-]{32,}/);
    expect(all).not.toContain('sb_secret_');
    expect(all).not.toContain('eyJ');
  });
});
