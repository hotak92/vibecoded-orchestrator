// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.49 access-matrix Phase 5 item #14 (F-2b) — vitest pinning the
// shape of the `kg_set_collection_access_mode` payload built from a
// "Grant write" / "Remove access" button click in
// CrossProjectAccessTab. The two assertions below are the
// regression-pins for the bug Item #14 fixes: the pre-v0.2.49 code
// hard-coded `mode: 'private'` via a dead ternary, so the
// "Remove access" button silently dispatched the same payload as
// "Grant write" — a no-op revoke.

import { describe, expect, it } from 'vitest';
import { buildOwnAccessPayload } from './cross-project-access-payloads';

describe('buildOwnAccessPayload (v0.2.49 F-2b)', () => {
  it('Remove access click → mode=\'none\' payload', () => {
    // The GUI dispatches mode='none' when the user clicks "Remove access"
    // on a row whose current state is 'read' or 'write'. Pre-v0.2.49 the
    // payload's `mode` was overwritten to 'private' by a dead ternary;
    // this assertion is the regression pin for that fix.
    const payload = buildOwnAccessPayload(
      'project-uuid-123',
      'OwnerProject_KnowledgeGraph',
      'none',
    );
    expect(payload).toEqual({
      owner_project_id: 'project-uuid-123',
      collection: 'OwnerProject_KnowledgeGraph',
      mode: 'none',
      project_ids: [],
    });
  });

  it('Grant write click → mode=\'private\' payload', () => {
    // The GUI dispatches mode='private' when the user clicks "Grant write"
    // on a row whose current state is 'none'. The mode survives the
    // payload-build unchanged (no translation).
    const payload = buildOwnAccessPayload(
      'project-uuid-123',
      'OwnerProject_KnowledgeGraph',
      'private',
    );
    expect(payload).toEqual({
      owner_project_id: 'project-uuid-123',
      collection: 'OwnerProject_KnowledgeGraph',
      mode: 'private',
      project_ids: [],
    });
  });

  it('payload carries the exact projectId + collection name unmodified', () => {
    // No URL-encoding, no quote-escaping, no trimming. The backend's
    // collection-name validator (commands::kg::kg_search and
    // friends) is the authoritative gate; the dispatch layer is a
    // pass-through.
    const payload = buildOwnAccessPayload(
      'WeIrD_Project-id.42',
      'Some_Collection_Name_With_Underscores',
      'private',
    );
    expect(payload.owner_project_id).toBe('WeIrD_Project-id.42');
    expect(payload.collection).toBe('Some_Collection_Name_With_Underscores');
  });

  it('project_ids is always an empty array for the per-row mutation', () => {
    // The per-row "Grant write" / "Remove access" path applies the
    // mutation to the owner + every peer (subject to backend's F-2c
    // user-configured preservation), not a curated subset. project_ids
    // is only used by mode='projects' which this surface does not
    // dispatch from these buttons.
    expect(
      buildOwnAccessPayload('p', 'C', 'none').project_ids,
    ).toEqual([]);
    expect(
      buildOwnAccessPayload('p', 'C', 'private').project_ids,
    ).toEqual([]);
  });
});
