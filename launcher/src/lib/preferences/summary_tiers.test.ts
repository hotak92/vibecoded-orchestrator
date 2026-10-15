// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.102 W5-D: the "No Ollama models installed" hint names the tier models
// from the ONE tier table (get_summary_model_info), never a literal list.

import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const calls: string[] = [];
vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(async (cmd: string) => {
    calls.push(cmd);
    return { is_set: false, value: null };
  }),
  safeInvoke: vi.fn(async (cmd: string) => {
    calls.push(cmd);
    return cmd === 'get_summary_model_info' ? PAYLOAD : null;
  }),
  listen: vi.fn(async () => () => {}),
}));
vi.mock('@tauri-apps/api/app', () => ({ getVersion: vi.fn(async () => '0.0.0') }));

// The shape `python -m vco_lib.summary_model --json` prints.
const PAYLOAD = {
  model: 'qwen3.5:4b',
  source: 'install-record',
  tiers: [
    { model: 'qwen3.5:9b', when: '9.1 GB+ VRAM' },
    { model: 'qwen3.5:4b', when: '4.9 GB+ VRAM' },
    { model: 'gemma4:e4b', when: 'no GPU, and a CPU with 12 GB+ RAM and 6+ cores' },
    { model: 'qwen3.5:0.8b', when: 'any other machine (on a GPU it needs 2.5 GB+ VRAM)' },
  ],
};

import { parseSummaryTiers } from './summary_tiers';
import { PREF_LOADERS } from './loaders';

const PAGE = readFileSync(
  fileURLToPath(new URL('../../routes/preferences/+page.svelte', import.meta.url)),
  'utf8',
);

describe('parseSummaryTiers', () => {
  it('keeps every tier, the floor included', () => {
    expect(parseSummaryTiers(PAYLOAD).map((t) => t.model)).toEqual([
      'qwen3.5:9b',
      'qwen3.5:4b',
      'gemma4:e4b',
      'qwen3.5:0.8b',
    ]);
  });

  it('a failed soft read or a malformed payload yields no list, never a guess', () => {
    expect(parseSummaryTiers(null)).toEqual([]);
    expect(parseSummaryTiers({ tiers: 'x' })).toEqual([]);
    expect(parseSummaryTiers({ tiers: [{ model: '' , when: '' }, { when: 'x' }, 3] })).toEqual([]);
  });
});

describe('wiring', () => {
  it('the kgSummary loader fetches the tier table', async () => {
    calls.length = 0;
    const res = (await PREF_LOADERS.kgSummary.load()) as unknown[];
    expect(calls).toContain('get_summary_model_info');
    expect(parseSummaryTiers(res[4]).length).toBe(3);
  });

  it('the hint renders the tiers and names no model literally', () => {
    const hint = PAGE.split('No Ollama models installed')[1].split('</p>')[0];
    expect(hint).toContain('{#each summaryTiers as tier');
    expect(hint).toContain('ollama pull {tier.model}');
    expect(hint).not.toMatch(/qwen3\.5|gemma4/);
    expect(PAGE).toContain('summaryTiers = parseSummaryTiers(modelInfo)');
  });
});
