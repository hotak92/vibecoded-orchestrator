// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// The one home for the "per-node access is not enforced yet" wording. The KG
// screen's per-node access modal writes `cross_project_access` onto nodes, but
// nothing filters search results by it yet (the MCP fan-out and the hub both
// ignore it). Enforcement is owner-deferred to v0.2.102, so the UI says so
// where it offers the control instead of implying a protection that is not
// there.

export const NODE_ACCESS_NOT_ENFORCED_NOTICE =
  'Per-node access is saved but not enforced yet: search results are not ' +
  'filtered by it. Enforcement is scheduled for v0.2.102 (owner decision).';

/** Only the per-node scopes carry the caveat; collection access is enforced. */
export function accessNoticeFor(
  kind: 'collection' | 'node' | 'node-bulk',
): string | null {
  return kind === 'collection' ? null : NODE_ACCESS_NOT_ENFORCED_NOTICE;
}
