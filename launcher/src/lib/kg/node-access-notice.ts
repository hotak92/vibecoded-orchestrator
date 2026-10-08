// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// The one home for the per-node access notice wording. The KG screen's
// per-node access modal writes `cross_project_access` onto nodes, and since
// v0.2.102 (census S2) the weaviate-kg MCP ENFORCES it on cross-project
// reads: the fan-out filters every non-self row carrying a restriction to
// the requesting project. The UI says so where it offers the control.

export const NODE_ACCESS_ENFORCED_NOTICE =
  'Enforced on cross-project reads: search fan-out filters rows restricted ' +
  "here (unrestricted rows and this project's own collection are unaffected).";

/** Only the per-node scopes carry the note; collection access has its own. */
export function accessNoticeFor(
  kind: 'collection' | 'node' | 'node-bulk',
): string | null {
  return kind === 'collection' ? null : NODE_ACCESS_ENFORCED_NOTICE;
}
