// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  RL_SCORING_LOCKED_NOTICE,
  RL_SCORING_SWITCH_LOCKED,
  rlScoringSwitchView,
} from './rl-scoring-default';

describe('RL-Scored Retrieval switch (owner 2026-10-01: wired, but inactive until trained)', () => {
  it('ships locked', () => {
    expect(RL_SCORING_SWITCH_LOCKED).toBe(true);
  });

  it('is DISABLED and explains why, even for a Pro licence', () => {
    const v = rlScoringSwitchView(false, true);
    expect(v.disabled).toBe(true);
    expect(v.notice).toBe(RL_SCORING_LOCKED_NOTICE);
    expect(v.notice).toMatch(/inactive until the model is trained/);
    expect(v.notice).toMatch(/Data collection continues/);
  });

  it('defaults OFF: no host-wide row (null/undefined) renders unchecked', () => {
    expect(rlScoringSwitchView(null, true).checked).toBe(false);
    expect(rlScoringSwitchView(undefined, true).checked).toBe(false);
  });

  it('renders the stored host-wide default (a later ON is shown, not hidden)', () => {
    expect(rlScoringSwitchView(true, true).checked).toBe(true);
  });

  it('unlocking (after training) leaves only the licence gate', () => {
    expect(rlScoringSwitchView(false, true, false)).toEqual({
      checked: false,
      disabled: false,
      notice: null,
    });
    expect(rlScoringSwitchView(false, false, false).disabled).toBe(true);
  });
});

describe('the dashboard is wired to the helper, not to a literal', () => {
  const src = readFileSync(
    fileURLToPath(new URL('./components/McpDashboard.svelte', import.meta.url)),
    'utf8',
  );
  it('renders disabled/checked from rlScoringSwitchView and writes through the setting command', () => {
    expect(src).toContain("rlScoringSwitchView(config.rl_retrieval_enabled, features.has_rl_retrieval)");
    expect(src).toContain('disabled={rlSwitch?.disabled ?? true}');
    expect(src).toContain("updateSetting('rl_retrieval_enabled'");
  });
});
