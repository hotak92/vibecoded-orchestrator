// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (#32) — tests for the New Project modal's Name↔Path coupling.
// PINS the browse-marks-path-touched guard (already present at base —
// the "browse loses the path" field diagnosis was retracted) so the
// reactive Name→Path sync can never re-derive over a browsed folder.
// These tests are the pin: do not delete them on the grounds that the
// original bug report was retracted.

import { describe, it, expect } from 'vitest';
import {
  autoDerivedPath,
  deriveAutoPath,
  onBrowsePicked,
  seedPathTouched,
  slugifyProjectName,
} from './project-selector-path-logic';

describe('onBrowsePicked + autoDerivedPath (the #32 guard, pinned)', () => {
  it('ACT: browse-then-rename keeps the browsed path', () => {
    // User browses to a real folder…
    const afterBrowse = onBrowsePicked(
      { path: '~/code/my-project', touched: false },
      '/home/user/projects/existing-app',
    );
    expect(afterBrowse).toEqual({
      path: '/home/user/projects/existing-app',
      touched: true,
    });
    // …then types a Name. The reactive sync must LEAVE the path alone.
    expect(
      autoDerivedPath(true, afterBrowse.touched, '~/code', 'Renamed App'),
    ).toBeNull();
  });

  it('LEAVE-ALONE: name typing with an untouched path still auto-derives', () => {
    expect(autoDerivedPath(true, false, '~/code', 'My App')).toBe(
      '~/code/my-app',
    );
    expect(autoDerivedPath(true, false, '/home/user/code', 'X')).toBe(
      '/home/user/code/x',
    );
  });

  it('a cancelled browse (null pick) changes nothing', () => {
    const state = { path: '~/code/my-project', touched: false };
    expect(onBrowsePicked(state, null)).toBe(state);
    // Still auto-derives afterwards — cancel must not count as touching.
    expect(autoDerivedPath(true, state.touched, '~/code', 'other')).toBe(
      '~/code/other',
    );
  });

  it('never derives while the modal is closed', () => {
    expect(autoDerivedPath(false, false, '~/code', 'name')).toBeNull();
  });
});

describe('seedPathTouched (openCreate leftover-path seeding — unchanged)', () => {
  it('a leftover path from a previous open counts as touched', () => {
    expect(seedPathTouched('/home/user/leftover')).toBe(true);
  });

  it('an empty or absent path seeds untouched (fresh derive)', () => {
    expect(seedPathTouched('')).toBe(false);
    expect(seedPathTouched(undefined)).toBe(false);
  });
});

describe('deriveAutoPath / slugifyProjectName', () => {
  it('falls back to ~/code and my-project when root/name are empty', () => {
    expect(deriveAutoPath('', '')).toBe('~/code/my-project');
  });

  it('slugifies to kebab-case, strips edge dashes, caps at 64 chars', () => {
    expect(slugifyProjectName('  My Great App!  ')).toBe('my-great-app');
    expect(slugifyProjectName('---x---')).toBe('x');
    expect(slugifyProjectName('a'.repeat(80)).length).toBe(64);
    expect(slugifyProjectName('keep.dots_and-dashes')).toBe(
      'keep.dots_and-dashes',
    );
  });
});
