// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.103: the Preferences "KG commit reviewer" selector — the options come
// from the Rust command's payload (the one rule table), the loader reads that
// command, and the page writes through `set_kg_reviewer_model`.

import { describe, it, expect, vi, beforeEach } from 'vitest';

const calls: Array<{ cmd: string; args: unknown }> = [];
vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(async (cmd: string, args?: unknown) => {
    calls.push({ cmd, args });
    return {
      model: 'sonnet',
      is_set: false,
      default: 'sonnet',
      choices: ['haiku', 'sonnet', 'opus'],
      env_override_key: 'VCO_KG_REVIEW_MODEL',
    };
  }),
  safeInvoke: vi.fn(async () => null),
  listen: vi.fn(async () => () => {}),
}));
vi.mock('@tauri-apps/api/app', () => ({ getVersion: vi.fn(async () => '0.0.0') }));

import { PREF_LOADERS } from './loaders';
import {
  kgReviewerModelHint,
  kgReviewerModelOptions,
  kgReviewerModelToastText,
  type KgReviewerModelInfo,
} from './kg_reviewer_model';

beforeEach(() => {
  calls.length = 0;
});

describe('KG commit reviewer model', () => {
  it('the loader reads get_kg_reviewer_model lazily', async () => {
    expect(PREF_LOADERS.kgReviewerModel.eager).toBe(false);
    const info = (await PREF_LOADERS.kgReviewerModel.load()) as KgReviewerModelInfo;
    expect(calls.map((c) => c.cmd)).toEqual(['get_kg_reviewer_model']);
    expect(info.default).toBe('sonnet');
  });

  it('options follow the payload order and mark the default', () => {
    const opts = kgReviewerModelOptions({ choices: ['haiku', 'sonnet', 'opus'], default: 'sonnet' });
    expect(opts.map((o) => o.value)).toEqual(['haiku', 'sonnet', 'opus']);
    expect(opts[1].label).toContain('default');
    expect(opts[0].label).not.toContain('default');
    // an unknown future choice still renders (by its id), never vanishes
    expect(kgReviewerModelOptions({ choices: ['x'], default: 'x' })[0].value).toBe('x');
  });

  it('the hint names the env override, the knowledge/-only scope and no flash fallback', () => {
    const hint = kgReviewerModelHint({ env_override_key: 'VCO_KG_REVIEW_MODEL' });
    expect(hint).toContain('VCO_KG_REVIEW_MODEL');
    expect(hint).toContain('knowledge/');
    expect(hint).toContain('never a flash model');
    expect(kgReviewerModelToastText('opus')).toContain('opus');
  });
});
