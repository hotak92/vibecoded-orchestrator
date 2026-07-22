// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Unit tests for the secret-grants API client's argument shaping. The
// backend commands take snake_case params (Tauri auto-maps camelCase JS
// args → snake_case Rust); these tests pin the camelCase arg keys, the
// per_project scope pin for pause/resume, and the default module bucket so
// a future rename of a command param is caught here rather than at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest';

// Capture (cmd, args) per call; the mocked invoke records + returns a stub.
const calls: Array<{ cmd: string; args: Record<string, unknown> | undefined }> = [];
let invokeReturn: unknown = undefined;

vi.mock('$lib/tauri', () => ({
  invoke: (cmd: string, args?: Record<string, unknown>) => {
    calls.push({ cmd, args });
    return Promise.resolve(invokeReturn);
  },
}));

afterEach(() => {
  calls.length = 0;
  invokeReturn = undefined;
});

async function client() {
  return import('./secret-grants');
}

describe('secret-grants API client', () => {
  it('listGrantsForProject passes camelCase projectId', async () => {
    invokeReturn = { issued: [], received: [] };
    const g = await client();
    await g.listGrantsForProject('p1');
    expect(calls[0].cmd).toBe('list_grants_for_project');
    expect(calls[0].args).toEqual({ projectId: 'p1' });
  });

  it('grantSecret shapes owner/grantee args and defaults the module bucket', async () => {
    invokeReturn = true;
    const g = await client();
    const inserted = await g.grantSecret({
      ownerProjectId: 'owner',
      key: 'MY_KEY',
      granteeProjectId: 'grantee',
    });
    expect(inserted).toBe(true);
    expect(calls[0].cmd).toBe('grant_secret');
    expect(calls[0].args).toEqual({
      ownerProjectId: 'owner',
      moduleId: g.GRANT_MODULE_BUCKET,
      key: 'MY_KEY',
      granteeProjectId: 'grantee',
      note: null,
    });
  });

  it('grantSecret forwards an explicit note', async () => {
    invokeReturn = true;
    const g = await client();
    await g.grantSecret({
      ownerProjectId: 'owner',
      key: 'K',
      granteeProjectId: 'grantee',
      note: 'shared for CI',
    });
    expect(calls[0].args?.note).toBe('shared for CI');
  });

  it('revokeSecretGrant shapes the revoke args', async () => {
    invokeReturn = true;
    const g = await client();
    await g.revokeSecretGrant({
      ownerProjectId: 'owner',
      key: 'K',
      granteeProjectId: 'grantee',
    });
    expect(calls[0].cmd).toBe('revoke_secret_grant_cmd');
    expect(calls[0].args).toEqual({
      ownerProjectId: 'owner',
      moduleId: g.GRANT_MODULE_BUCKET,
      key: 'K',
      granteeProjectId: 'grantee',
    });
  });

  it('pauseSecretForProject pins scope=per_project and shapes requester arg', async () => {
    const g = await client();
    await g.pauseSecretForProject({
      projectId: 'owner',
      key: 'K',
      requesterProjectId: 'req',
    });
    expect(calls[0].cmd).toBe('pause_secret_for_project');
    expect(calls[0].args).toEqual({
      scope: 'per_project',
      projectId: 'owner',
      moduleId: g.GRANT_MODULE_BUCKET,
      key: 'K',
      requesterProjectId: 'req',
    });
  });

  it('resumeSecretForProject pins scope=per_project and shapes requester arg', async () => {
    const g = await client();
    await g.resumeSecretForProject({
      projectId: 'owner',
      key: 'K',
      requesterProjectId: 'req',
    });
    expect(calls[0].cmd).toBe('resume_secret_for_project');
    expect(calls[0].args).toEqual({
      scope: 'per_project',
      projectId: 'owner',
      moduleId: g.GRANT_MODULE_BUCKET,
      key: 'K',
      requesterProjectId: 'req',
    });
  });

  it('isSecretPausedForRequester returns the backend bool and shapes args', async () => {
    invokeReturn = true;
    const g = await client();
    const paused = await g.isSecretPausedForRequester({
      projectId: 'owner',
      key: 'K',
      requesterProjectId: 'req',
    });
    expect(paused).toBe(true);
    expect(calls[0].cmd).toBe('is_secret_paused_for_requester');
    expect(calls[0].args).toEqual({
      scope: 'per_project',
      projectId: 'owner',
      moduleId: g.GRANT_MODULE_BUCKET,
      key: 'K',
      requesterProjectId: 'req',
    });
  });
});
