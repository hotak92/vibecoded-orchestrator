// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 W5R-04: the extra-path "stale" badge words the server-side
// `stale` flag (Rust `extra_path_is_stale`, the rule the Stop-hook refresh
// also uses). Act + leave-alone cases.

import { describe, expect, it } from 'vitest';
import { extraPathStaleBadge, type ExtraPath } from './codegraph-extras';

function row(over: Partial<ExtraPath>): ExtraPath {
  return {
    project_id: 'p1',
    path: '/srv/clone',
    label: null,
    added_at: 1,
    last_indexed_at: 2,
    last_indexed_commit: 'aaaaaaaaaaaa',
    enabled: true,
    display_label: 'clone',
    head_commit: 'bbbbbbbbbbbb',
    stale: true,
    ...over,
  };
}

describe('extraPathStaleBadge', () => {
  it('badges an enabled stale path, naming both commits', () => {
    const b = extraPathStaleBadge(row({}));
    expect(b?.text).toBe('stale');
    expect(b?.title).toContain('bbbbbbbb');
    expect(b?.title).toContain('aaaaaaaa');
  });

  it('names a never-indexed path as such', () => {
    const b = extraPathStaleBadge(row({ last_indexed_commit: null }));
    expect(b?.title).toContain('indexed at never');
  });

  it('shows nothing for an up-to-date path', () => {
    expect(extraPathStaleBadge(row({ stale: false }))).toBeNull();
  });

  it('shows nothing for a disabled path (nothing re-indexes it)', () => {
    expect(extraPathStaleBadge(row({ enabled: false }))).toBeNull();
  });

  it('shows nothing when the server sent no stale flag (older payload)', () => {
    expect(extraPathStaleBadge(row({ stale: undefined }))).toBeNull();
  });
});
