// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.47: unit tests for the codegraph-extras API wrapper.
//
// These tests verify the wire shape we promise to Agent A's Tauri
// commands — argv names, args ordering, default values. They are pure
// unit tests: the `$lib/tauri` module is mocked so no Tauri runtime is
// required.

import { beforeEach, describe, expect, it, vi } from 'vitest';

// Hoisted mock — must register before the SUT imports.
vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(),
}));

import { invoke } from '$lib/tauri';
import {
  addExtraPath,
  grantCodegraphReadAccess,
  listExtraPaths,
  reindexAfterExtrasChange,
  removeExtraPath,
  setExtraPathEnabled,
  syncExtraPath,
} from './codegraph_extras';
import type { ExtraPath, SyncOutcome } from '$lib/types/codegraph-extras';

const mockInvoke = invoke as unknown as ReturnType<typeof vi.fn>;

function makeRow(overrides: Partial<ExtraPath> = {}): ExtraPath {
  return {
    project_id: 'proj-1',
    path: '/abs/path',
    label: null,
    added_at: 1_700_000_000_000,
    last_indexed_at: null,
    last_indexed_commit: null,
    enabled: true,
    display_label: 'path',
    ...overrides,
  };
}

function makeOutcome(overrides: Partial<SyncOutcome> = {}): SyncOutcome {
  return {
    files_scanned: 100,
    entities_indexed: 250,
    duration_ms: 1234,
    project_codegraph_prefix: 'TestProject',
    ...overrides,
  };
}

beforeEach(() => {
  mockInvoke.mockReset();
});

describe('listExtraPaths', () => {
  it('invokes the canonical command name with snake_case projectId', async () => {
    mockInvoke.mockResolvedValueOnce([makeRow()]);
    const result = await listExtraPaths('proj-1');
    expect(mockInvoke).toHaveBeenCalledWith(
      'list_project_codegraph_extra_paths',
      { projectId: 'proj-1' },
    );
    expect(result).toHaveLength(1);
  });

  it('forwards an empty array on no rows', async () => {
    mockInvoke.mockResolvedValueOnce([]);
    const result = await listExtraPaths('proj-empty');
    expect(result).toEqual([]);
  });
});

describe('addExtraPath', () => {
  it('defaults force=false and label=null', async () => {
    mockInvoke.mockResolvedValueOnce({ action: 'added', row: makeRow() });
    await addExtraPath('proj-1', '/abs/path');
    expect(mockInvoke).toHaveBeenCalledWith(
      'add_project_codegraph_extra_path',
      {
        projectId: 'proj-1',
        path: '/abs/path',
        label: null,
        force: false,
      },
    );
  });

  it('forwards force=true on the bypass path', async () => {
    mockInvoke.mockResolvedValueOnce({ action: 'added', row: makeRow() });
    await addExtraPath('proj-1', '/abs/path', { force: true });
    expect(mockInvoke).toHaveBeenCalledWith(
      'add_project_codegraph_extra_path',
      expect.objectContaining({ force: true }),
    );
  });

  it('returns the disambiguation variant verbatim', async () => {
    const disambig = {
      action: 'disambiguation_required' as const,
      existing_project: {
        id: 'proj-other',
        name: 'Other',
        slug: 'other',
        folder_path: '/abs/path',
      },
      path: '/abs/path',
    };
    mockInvoke.mockResolvedValueOnce(disambig);
    const result = await addExtraPath('proj-1', '/abs/path');
    expect(result).toEqual(disambig);
  });
});

describe('removeExtraPath', () => {
  it('invokes the delete command', async () => {
    mockInvoke.mockResolvedValueOnce(undefined);
    await removeExtraPath('proj-1', '/abs/path');
    expect(mockInvoke).toHaveBeenCalledWith(
      'remove_project_codegraph_extra_path',
      { projectId: 'proj-1', path: '/abs/path' },
    );
  });
});

describe('setExtraPathEnabled', () => {
  it('forwards enabled=true', async () => {
    mockInvoke.mockResolvedValueOnce(undefined);
    await setExtraPathEnabled('proj-1', '/abs/path', true);
    expect(mockInvoke).toHaveBeenCalledWith(
      'set_project_codegraph_extra_path_enabled',
      { projectId: 'proj-1', path: '/abs/path', enabled: true },
    );
  });

  it('forwards enabled=false', async () => {
    mockInvoke.mockResolvedValueOnce(undefined);
    await setExtraPathEnabled('proj-1', '/abs/path', false);
    expect(mockInvoke).toHaveBeenCalledWith(
      'set_project_codegraph_extra_path_enabled',
      expect.objectContaining({ enabled: false }),
    );
  });
});

describe('syncExtraPath', () => {
  it('passes incremental flag through', async () => {
    mockInvoke.mockResolvedValueOnce(makeOutcome());
    await syncExtraPath('proj-1', '/abs/path', true);
    expect(mockInvoke).toHaveBeenCalledWith(
      'sync_project_codegraph_extra_path',
      { projectId: 'proj-1', path: '/abs/path', incremental: true },
    );
  });
});

describe('reindexAfterExtrasChange', () => {
  it('forwards prune_stale as pruneStale (camelCase arg name)', async () => {
    mockInvoke.mockResolvedValueOnce(makeOutcome());
    await reindexAfterExtrasChange('proj-1', true);
    expect(mockInvoke).toHaveBeenCalledWith(
      'reindex_project_codegraph_after_extras_change',
      { projectId: 'proj-1', pruneStale: true },
    );
  });
});

describe('grantCodegraphReadAccess', () => {
  it('wraps the request in the `req` envelope expected by the existing access-matrix command', async () => {
    mockInvoke.mockResolvedValueOnce(undefined);
    // grantor = existing project (whose codegraph is being shared);
    // grantee = current project (which gains READ access).
    await grantCodegraphReadAccess('proj-existing', 'proj-current');
    expect(mockInvoke).toHaveBeenCalledWith('codegraph_grant_access', {
      req: {
        grantor_project_id: 'proj-existing',
        grantee_project_id: 'proj-current',
        access_level: 'read',
      },
    });
  });
});
