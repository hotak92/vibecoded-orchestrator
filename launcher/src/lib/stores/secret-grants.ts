// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
// Cross-project secret-grant + per-requester-pause API client.
//
// One home for the grant/pause commands so panels reuse it instead of
// copying invoke shapes. Backs the SecretsPanel grants section. All calls
// target the per_project scope — the only scope where cross-project grants
// and per-requester pause are meaningful (shared/global secrets are already
// cross-project, so the backend rejects grant_secret for owner==grantee and
// pins scope='per_project' internally).
//
// The UI-created secret bucket is module_id='user' (see SecretsPanel's
// UI_MODULE_BUCKET); grants + pauses target that bucket by default.

import { invoke } from '$lib/tauri';

/** The module bucket UI-created secrets land in. Mirrors SecretsPanel's
 *  UI_MODULE_BUCKET — kept here so grant callers don't re-derive it. */
export const GRANT_MODULE_BUCKET = 'user';

/** One grant row as returned by `list_grants_for_project`. Mirrors the
 *  Rust `SecretGrantView` (secrets_cmd.rs). */
export interface SecretGrant {
  scope: string;
  owner_project_id: string;
  module_id: string;
  key: string;
  grantee_project_id: string;
  granted_at: number;
  granted_by_actor: string | null;
  note: string | null;
}

/** Grants split by direction, per `ProjectGrantsView` (Rust). `issued` =
 *  grants this project made as the OWNER; `received` = grants where this
 *  project is the GRANTEE. */
export interface ProjectGrants {
  issued: SecretGrant[];
  received: SecretGrant[];
}

/** List every grant this project issued (as owner) or received (as
 *  grantee). */
export async function listGrantsForProject(projectId: string): Promise<ProjectGrants> {
  return invoke<ProjectGrants>('list_grants_for_project', { projectId });
}

/** Grant a per-project secret KEY owned by `ownerProjectId` to
 *  `granteeProjectId`. Returns true when a new grant row was inserted
 *  (false when it already existed). */
export async function grantSecret(args: {
  ownerProjectId: string;
  key: string;
  granteeProjectId: string;
  moduleId?: string;
  note?: string | null;
}): Promise<boolean> {
  return invoke<boolean>('grant_secret', {
    ownerProjectId: args.ownerProjectId,
    moduleId: args.moduleId ?? GRANT_MODULE_BUCKET,
    key: args.key,
    granteeProjectId: args.granteeProjectId,
    note: args.note ?? null,
  });
}

/** Revoke a previously-issued grant. Returns true when a row was removed. */
export async function revokeSecretGrant(args: {
  ownerProjectId: string;
  key: string;
  granteeProjectId: string;
  moduleId?: string;
}): Promise<boolean> {
  return invoke<boolean>('revoke_secret_grant_cmd', {
    ownerProjectId: args.ownerProjectId,
    moduleId: args.moduleId ?? GRANT_MODULE_BUCKET,
    key: args.key,
    granteeProjectId: args.granteeProjectId,
  });
}

/** Pause a secret for one specific requester project: the
 *  (scope, key, requester) row is marked inactive so the secret stops
 *  resolving for that requester only. */
export async function pauseSecretForProject(args: {
  projectId: string;
  key: string;
  requesterProjectId: string;
  scope?: string;
  moduleId?: string;
}): Promise<void> {
  await invoke<void>('pause_secret_for_project', {
    scope: args.scope ?? 'per_project',
    projectId: args.projectId,
    moduleId: args.moduleId ?? GRANT_MODULE_BUCKET,
    key: args.key,
    requesterProjectId: args.requesterProjectId,
  });
}

/** Resume a paused secret for one requester: drops the per-requester
 *  inactive row so the canonical active state takes over again. */
export async function resumeSecretForProject(args: {
  projectId: string;
  key: string;
  requesterProjectId: string;
  scope?: string;
  moduleId?: string;
}): Promise<void> {
  await invoke<void>('resume_secret_for_project', {
    scope: args.scope ?? 'per_project',
    projectId: args.projectId,
    moduleId: args.moduleId ?? GRANT_MODULE_BUCKET,
    key: args.key,
    requesterProjectId: args.requesterProjectId,
  });
}

/** Whether a secret is currently PAUSED for one requester (true = paused).
 *  Lets the grants UI render Pause vs Resume correctly on load. */
export async function isSecretPausedForRequester(args: {
  projectId: string;
  key: string;
  requesterProjectId: string;
  scope?: string;
  moduleId?: string;
}): Promise<boolean> {
  return invoke<boolean>('is_secret_paused_for_requester', {
    scope: args.scope ?? 'per_project',
    projectId: args.projectId,
    moduleId: args.moduleId ?? GRANT_MODULE_BUCKET,
    key: args.key,
    requesterProjectId: args.requesterProjectId,
  });
}
