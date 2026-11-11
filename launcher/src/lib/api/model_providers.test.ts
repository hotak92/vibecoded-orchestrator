// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.105 — wire-shape + wording tests for the Home page's model-provider
// switch (`$lib/api/model_providers`).
//
// Two things are pinned without a DOM:
//   1. the Tauri wire shape — command names and argument names, so the card
//      cannot drift from `commands/model_providers.rs` (a rename is a runtime
//      "command not found" the type checker cannot see);
//   2. the WORDING decisions — the key state is shown as words (never a key
//      value), and a `stored: false` answer is reported as "nothing was
//      changed" rather than as applied.

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({ invoke: vi.fn() }));

import { invoke } from '$lib/tauri';
import {
  anyDisabled,
  applyOutcomeLabel,
  cardVisible,
  definitionsLabel,
  getModelProviders,
  keyStateLabel,
  PROVIDER_OFF_NOTICE,
  secretNamesLabel,
  setModelProviderEnabled,
  toggleNote,
  type ModelProviderRow,
  type ModelProvidersPayload,
  type ProviderApplySummary,
} from './model_providers';

const mockInvoke = invoke as unknown as ReturnType<typeof vi.fn>;

function row(over: Partial<ModelProviderRow> = {}): ModelProviderRow {
  return {
    vendor_id: 'kimi',
    display: 'Kimi Code',
    enabled: true,
    key_state: 'present',
    secret_key_names: ['kimi_api_key', 'KIMI_API_KEY'],
    docs_url: 'https://example.invalid/kimi',
    upstream_host: 'api.kimi.ai',
    definitions: ['kimi-implementer', 'kimi-reviewer'],
    ...over,
  };
}

function payload(over: Partial<ModelProvidersPayload> = {}): ModelProvidersPayload {
  return {
    disabled: [],
    gateway_available: true,
    providers: [row()],
    app_state_key: 'model_providers.disabled',
    ...over,
  };
}

/** The apply summary a stored `set` carries (v0.2.106 — the immediate leg). */
function applySummary(over: Partial<ProviderApplySummary> = {}): ProviderApplySummary {
  return {
    vendor: 'kimi',
    enabled: false,
    held: false,
    held_reason: '',
    parked_projects: 0,
    restored_projects: 0,
    failed_projects: 0,
    projects: [],
    ...over,
  };
}

beforeEach(() => mockInvoke.mockReset());

describe('the Tauri wire shape', () => {
  it('reads through model_providers_status', async () => {
    mockInvoke.mockResolvedValue(payload());
    const answer = await getModelProviders();
    expect(mockInvoke).toHaveBeenCalledWith('model_providers_status');
    expect(answer.providers[0].vendor_id).toBe('kimi');
  });

  it('writes through model_providers_set with vendor + enabled', async () => {
    mockInvoke.mockResolvedValue({ vendor: 'kimi', enabled: false, stored: true });
    const answer = await setModelProviderEnabled('kimi', false);
    expect(mockInvoke).toHaveBeenCalledWith('model_providers_set', {
      vendor: 'kimi',
      enabled: false,
    });
    expect(answer.stored).toBe(true);
  });
});

describe('what the card shows', () => {
  it('names the key state in words, never a value', () => {
    expect(keyStateLabel('present').text).toBe('key detected');
    expect(keyStateLabel('absent').text).toBe('no key');
    expect(keyStateLabel('unavailable').text).toBe('key store unavailable');
    expect(keyStateLabel('unknown').text).toBe('key state unknown');
  });

  it('shows the declared key NAMES and the agent count', () => {
    expect(secretNamesLabel(row())).toBe('kimi_api_key or KIMI_API_KEY');
    expect(secretNamesLabel(row({ secret_key_names: [] }))).toBe(
      'no key name declared',
    );
    expect(definitionsLabel(row())).toBe('2 agents');
    expect(definitionsLabel(row({ definitions: ['kimi-reviewer'] }))).toBe('1 agent');
    expect(definitionsLabel(row({ definitions: [] }))).toBe('no agent definitions');
  });

  it('renders nothing when the vendor table could not be read', () => {
    expect(cardVisible(null)).toBe(false);
    expect(cardVisible(payload({ gateway_available: false, providers: [] }))).toBe(false);
    expect(cardVisible(payload({ providers: [] }))).toBe(false);
    expect(cardVisible(payload())).toBe(true);
  });

  it('reports any switched-off provider', () => {
    expect(anyDisabled(payload())).toBe(false);
    expect(anyDisabled(payload({ providers: [row({ enabled: false })] }))).toBe(true);
    expect(anyDisabled(null)).toBe(false);
  });
});

describe('the sentence after a toggle', () => {
  it('explains exactly what "off" does — parked, not deleted, rows hidden', () => {
    const note = toggleNote(row(), false, {
      vendor: 'kimi',
      enabled: false,
      stored: true,
      agents: applySummary({ parked_projects: 2 }),
    });
    expect(note).toContain('Kimi Code is off');
    expect(note).toContain('parked in 2 projects');
    expect(note).toContain('.claude/agents.disabled/');
    expect(note).toContain('nothing is deleted');
    expect(note).toContain('model guidance');
    expect(note.endsWith(PROVIDER_OFF_NOTICE)).toBe(true);
  });

  it('the standing notice names the gateway leg too', () => {
    // Off is a full stop: agents parked, rows hidden, the running gateway
    // stops serving — the card says all three.
    expect(PROVIDER_OFF_NOTICE).toContain('.claude/agents.disabled/');
    expect(PROVIDER_OFF_NOTICE).toContain('gateway');
    expect(PROVIDER_OFF_NOTICE).not.toContain('next update');
  });

  it('says what turning one back on does — restored at once, no update wait', () => {
    const note = toggleNote(row(), true, {
      vendor: 'kimi',
      enabled: true,
      stored: true,
      agents: applySummary({ enabled: true, restored_projects: 3 }),
    });
    expect(note).toContain('is on again');
    expect(note).toContain('restored in 3 projects');
    expect(note).not.toContain('next update');
    expect(note).toContain('model guidance');
  });

  it('a re-enable says when restored definitions were brought to this release', () => {
    const summary = applySummary({ enabled: true, restored_projects: 1 });
    summary.projects = [
      {
        project: 'Alpha',
        folder: '/w/alpha',
        status: 'ok',
        parked: [],
        already_parked: [],
        kept: [],
        restored: ['kimi-reviewer.md', 'kimi-implementer.md'],
        refreshed: ['kimi-reviewer.md'],
        notes: [],
        error: '',
      },
    ];
    const label = applyOutcomeLabel({ vendor: 'kimi', enabled: true, stored: true, agents: summary });
    expect(label).toContain('restored in 1 project');
    expect(label).toContain("1 of them came from an older release and was brought to this release's");
    expect(label).toContain('.claude/backups/bundle-adoptions/');
  });

  it('a re-enable whose key does not resolve keeps the definitions parked', () => {
    const note = toggleNote(row(), true, {
      vendor: 'kimi',
      enabled: true,
      stored: true,
      agents: applySummary({
        enabled: true,
        held: true,
        held_reason: "no 'kimi' vendor key resolved",
      }),
    });
    expect(note).toContain('stay parked');
    expect(note).toContain('key');
    expect(note).not.toContain('restored in');
  });

  it('a partially failed follow-up is named, not hidden', () => {
    const note = toggleNote(row(), false, {
      vendor: 'kimi',
      enabled: false,
      stored: true,
      agents: applySummary({ parked_projects: 1, failed_projects: 1 }),
    });
    expect(note).toContain('parked in 1 project');
    expect(note).toContain('failed in 1 project');
  });

  it('an unreadable project list is named, never read as "every project"', () => {
    const label = applyOutcomeLabel({
      vendor: 'kimi',
      enabled: false,
      stored: true,
      agents: applySummary({
        parked_projects: 1,
        failed_projects: 1,
        registry_error: "the launcher's project list could not be read",
      }),
    });
    expect(label).toContain('failed in 1 project');
    expect(label).toContain('project list could not be read');
  });

  it('the outcome label stands alone for a missing summary', () => {
    expect(applyOutcomeLabel({ vendor: 'kimi', enabled: false, stored: true })).toContain(
      'did not report',
    );
  });

  it('never reports an unstored change as applied', () => {
    const note = toggleNote(row(), false, {
      vendor: 'kimi',
      enabled: false,
      stored: false,
    });
    expect(note).toContain('Could not save');
    expect(note).toContain('Nothing was changed');
    expect(note).not.toContain(PROVIDER_OFF_NOTICE);
  });

  it('a failed call never claims nothing changed, and reports the re-read state', () => {
    const stuck = toggleNote(row(), false, null, true);
    expect(stuck).toContain('Could not ask');
    expect(stuck).toContain('still on');
    expect(stuck).not.toContain('Nothing was changed');
    const landed = toggleNote(row(), false, null, false);
    expect(landed).toContain('now reads off');
    expect(landed).not.toContain('Nothing was changed');
    const unknown = toggleNote(row(), false, null, null);
    expect(unknown).toContain('could not be read back');
  });
});