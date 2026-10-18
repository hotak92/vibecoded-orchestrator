// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.104: the homepage "Anthropic: OAuth | API key" switch — the
// disabled-reasons and warnings live in `$lib/api/model_gateway` so they are
// testable without mounting the frame.

import { describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({ invoke: vi.fn(), tauriAvailable: () => true }));

import { invoke } from '$lib/tauri';
import {
  ANTHROPIC_AUTH_MODES,
  anthropicAuthDisabledReason,
  anthropicAuthWarning,
  getAnthropicAuthMode,
  setAnthropicAuthMode,
} from './model_gateway';
import type { ModelGatewayStatus } from '$lib/types/model-gateway';

function gw(auth: Record<string, unknown> | null, live = true): ModelGatewayStatus {
  return {
    reachable: live,
    health: {
      service: 'vct-model-gateway',
      ...(auth ? { anthropic_auth: auth } : {}),
    },
  } as unknown as ModelGatewayStatus;
}

const AUTH = (api_key: string, mode = 'oauth') => ({
  mode,
  api_key,
  api_key_fallback: false,
  api_key_secret: 'ANTHROPIC_API_KEY_GATEWAY',
});

describe('anthropicAuthDisabledReason', () => {
  it('OAuth is always selectable', () => {
    expect(anthropicAuthDisabledReason('oauth', null)).toBe('');
  });

  it('API key is selectable only when the gateway says the key resolves', () => {
    expect(anthropicAuthDisabledReason('api_key', gw(AUTH('available')))).toBe('');
  });

  it('a missing key disables the choice and names the secret', () => {
    const why = anthropicAuthDisabledReason('api_key', gw(AUTH('missing')));
    expect(why).toContain('ANTHROPIC_API_KEY_GATEWAY');
  });

  it('a key Anthropic rejected disables the choice and says so', () => {
    expect(anthropicAuthDisabledReason('api_key', gw(AUTH('rejected')))).toMatch(/rejected/);
  });

  it('an older running gateway names the restart as the remedy', () => {
    expect(anthropicAuthDisabledReason('api_key', gw(null))).toMatch(/restart/);
  });

  it('a not-yet-resolved key, a stopped or an older gateway all disable it', () => {
    expect(anthropicAuthDisabledReason('api_key', gw(AUTH('unknown')))).not.toBe('');
    expect(anthropicAuthDisabledReason('api_key', gw(AUTH('available'), false))).not.toBe('');
    expect(anthropicAuthDisabledReason('api_key', gw(null))).not.toBe('');
  });
});

describe('anthropicAuthWarning', () => {
  it('says so when API key mode is stored but no key resolves (refused, not OAuth)', () => {
    expect(anthropicAuthWarning('api_key', gw(AUTH('missing', 'api_key')))).toMatch(/refused/);
  });

  it('warns when API key mode is on and Anthropic rejected the key', () => {
    expect(anthropicAuthWarning('api_key', gw(AUTH('rejected', 'api_key')))).toMatch(/rejected the key/);
  });

  it('says the gateway cannot read the launcher settings instead of promising seconds', () => {
    const auth = {
      ...AUTH('available', 'api_key'),
      launcher_db: 'unreadable',
      launcher_db_error: 'OperationalError: database is locked',
    };
    const warning = anthropicAuthWarning('oauth', gw(auth));
    expect(warning).toMatch(/can't read the launcher settings/);
    expect(warning).toMatch(/keeps using api_key/);
    expect(warning).toMatch(/database is locked/);
    expect(warning).not.toMatch(/within a few seconds/);
  });

  it('is quiet when the gateway applies the stored mode', () => {
    expect(anthropicAuthWarning('api_key', gw(AUTH('available', 'api_key')))).toBeNull();
    expect(anthropicAuthWarning('oauth', gw(AUTH('available', 'oauth')))).toBeNull();
  });

  it('reports a gateway still on the other mode, and an older gateway', () => {
    expect(anthropicAuthWarning('api_key', gw(AUTH('available', 'oauth')))).toMatch(/oauth/);
    expect(anthropicAuthWarning('api_key', gw(null))).toMatch(/restart the gateway/);
    expect(anthropicAuthWarning('oauth', gw(null))).toBeNull();
  });
});

describe('the commands', () => {
  it('get/set go through the two registered command names', async () => {
    vi.mocked(invoke).mockResolvedValueOnce('api_key');
    expect(await getAnthropicAuthMode()).toBe('api_key');
    expect(invoke).toHaveBeenLastCalledWith('model_gateway_anthropic_auth_get');
    vi.mocked(invoke).mockResolvedValueOnce('oauth');
    expect(await setAnthropicAuthMode('oauth')).toBe('oauth');
    expect(invoke).toHaveBeenLastCalledWith('model_gateway_anthropic_auth_set', { mode: 'oauth' });
  });

  it('the mode list is a value list, OAuth first (the default)', () => {
    expect(ANTHROPIC_AUTH_MODES).toEqual(['oauth', 'api_key']);
  });
});
