// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.49 access-matrix overhaul, Phase 6 S-4 (Stream W3).
//
// Vitest pins for the pure-TS folder-health helpers consumed by
// ProjectCard.svelte. The Svelte component renders the banner
// strictly when `shouldShowFolderMissingBanner` returns true, so
// pinning this predicate also pins the rendering condition.

import { describe, expect, it } from 'vitest';
import {
  buildFolderMissingMap,
  folderMissingBannerCopy,
  shouldShowFolderMissingBanner,
  type ProjectFolderFlag,
} from './project-folder-health';

function mkFlag(partial: Partial<ProjectFolderFlag>): ProjectFolderFlag {
  return {
    id: 'p1',
    folder_path: '/tmp/test-folder',
    folder_missing_at_last_boot: false,
    ...partial,
  };
}

describe('shouldShowFolderMissingBanner', () => {
  it('returns true only when the flag is explicitly true', () => {
    expect(
      shouldShowFolderMissingBanner(mkFlag({ folder_missing_at_last_boot: true })),
    ).toBe(true);
  });

  it('returns false when the flag is false', () => {
    expect(
      shouldShowFolderMissingBanner(mkFlag({ folder_missing_at_last_boot: false })),
    ).toBe(false);
  });

  it('returns false for null / undefined input', () => {
    expect(shouldShowFolderMissingBanner(null)).toBe(false);
    expect(shouldShowFolderMissingBanner(undefined)).toBe(false);
  });
});

describe('folderMissingBannerCopy', () => {
  it('embeds the folder path verbatim so users can copy it', () => {
    const copy = folderMissingBannerCopy(
      mkFlag({ folder_path: '/home/martino/projects/gone' }),
    );
    expect(copy).toContain('/home/martino/projects/gone');
  });

  it('uses a clear "moved or deleted" prompt — pinned wording', () => {
    const copy = folderMissingBannerCopy(mkFlag({}));
    expect(copy).toContain('Folder not found at');
    expect(copy).toContain('Did you move or delete it?');
  });
});

describe('buildFolderMissingMap', () => {
  it('produces a quick-lookup id → flag map', () => {
    const map = buildFolderMissingMap([
      mkFlag({ id: 'p1', folder_missing_at_last_boot: true }),
      mkFlag({ id: 'p2', folder_missing_at_last_boot: false }),
      mkFlag({ id: 'p3', folder_missing_at_last_boot: true }),
    ]);
    expect(map).toEqual({ p1: true, p2: false, p3: true });
  });

  it('returns an empty map on empty input', () => {
    expect(buildFolderMissingMap([])).toEqual({});
  });

  it('handles a single-element list', () => {
    expect(buildFolderMissingMap([mkFlag({ id: 'only', folder_missing_at_last_boot: true })]))
      .toEqual({ only: true });
  });
});
