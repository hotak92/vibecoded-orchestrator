// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import { rlScoringSwitchView } from './rl-scoring-default';
import { renderSvelte } from './test-support/svelte-ssr';

// The shipped lock lives in ONE place: vco_lib/rl_scoring_lock.toml. The GUI
// never holds a copy; this test reads the same file to build realistic input.
const LOCK_TOML = readFileSync(
  fileURLToPath(new URL('../../../vco_lib/rl_scoring_lock.toml', import.meta.url)),
  'utf8',
);
const SHIPPED_REASON = /^reason\s*=\s*"([^"]+)"/m.exec(LOCK_TOML)![1];

describe('RL-Scored Retrieval switch view (W5R-02: render the truth)', () => {
  it('the shipped table is locked (owner 2026-10-01)', () => {
    expect(/^locked\s*=\s*true\s*$/m.test(LOCK_TOML)).toBe(true);
    expect(SHIPPED_REASON).toMatch(/until the model is trained/);
  });

  it('locked: never checked, even with a stored host-wide ON and a Pro licence', () => {
    const v = rlScoringSwitchView(true, true, SHIPPED_REASON);
    expect(v.checked).toBe(false);
    expect(v.disabled).toBe(true);
    expect(v.notice).toBe(SHIPPED_REASON);
    expect(v.storedNote).toMatch(/Stored host-wide default: On.*applies once/);
  });

  it('locked with no stored ON: no stored note', () => {
    expect(rlScoringSwitchView(null, true, SHIPPED_REASON).storedNote).toBeNull();
    expect(rlScoringSwitchView(false, true, SHIPPED_REASON).storedNote).toBeNull();
  });

  it('lock not loaded (undefined): disabled and unchecked, never a guess', () => {
    expect(rlScoringSwitchView(true, true, undefined)).toEqual({
      checked: false,
      disabled: true,
      notice: null,
      storedNote: null,
    });
  });

  it('unlocked: the stored row and the licence decide', () => {
    expect(rlScoringSwitchView(true, true, null)).toEqual({
      checked: true,
      disabled: false,
      notice: null,
      storedNote: null,
    });
    expect(rlScoringSwitchView(null, true, null).checked).toBe(false);
    expect(rlScoringSwitchView(false, false, null).disabled).toBe(true);
  });
});

const SWITCH = fileURLToPath(new URL('./components/RlScoringSwitch.svelte', import.meta.url));

describe('RlScoringSwitch renders the view as DOM state (W5R-09)', () => {
  it('locked + stored ON + Pro: the input is disabled and NOT checked', async () => {
    const html = await renderSvelte(SWITCH, {
      view: rlScoringSwitchView(true, true, SHIPPED_REASON),
      onToggle: () => {},
    });
    const input = /<input[^>]*data-testid="rl-scoring-switch"[^>]*>/.exec(html)![0];
    expect(input).toMatch(/\sdisabled(=""|\s|\/|>)/);
    expect(input).not.toMatch(/\schecked/);
  });

  it('unlocked + stored ON + Pro: checked and enabled (the render is not constant)', async () => {
    const html = await renderSvelte(SWITCH, {
      view: rlScoringSwitchView(true, true, null),
      onToggle: () => {},
    });
    const input = /<input[^>]*data-testid="rl-scoring-switch"[^>]*>/.exec(html)![0];
    expect(input).toMatch(/\schecked/);
    expect(input).not.toMatch(/\sdisabled/);
  });
});

describe('McpDashboard mounts the switch with the lock-aware view', () => {
  const src = readFileSync(
    fileURLToPath(new URL('./components/McpDashboard.svelte', import.meta.url)),
    'utf8',
  );
  const ast = parse(src, { modern: true });

  function findComponent(node: unknown, name: string): Record<string, unknown> | null {
    if (!node || typeof node !== 'object') return null;
    const n = node as Record<string, unknown>;
    if (n.type === 'Component' && n.name === name) return n;
    for (const v of Object.values(n)) {
      const hit = Array.isArray(v)
        ? v.map((x) => findComponent(x, name)).find(Boolean) ?? null
        : findComponent(v, name);
      if (hit) return hit;
    }
    return null;
  }

  it('<RlScoringSwitch view={rlSwitch}> and rlSwitch is derived with the served lock', () => {
    const comp = findComponent(ast.fragment, 'RlScoringSwitch');
    expect(comp).not.toBeNull();
    const attrs = (comp!.attributes as Array<Record<string, unknown>>).map((a) => [
      a.name,
      src.slice((a.value as { start: number }).start, (a.value as { end: number }).end),
    ]);
    expect(attrs).toContainEqual(['view', '{rlSwitch}']);
    expect(src).toMatch(
      /rlScoringSwitchView\(config\.rl_retrieval_enabled,\s*features\.has_rl_retrieval,\s*rlLock\)/,
    );
    expect(src).toMatch(/invoke<string \| null>\('rl_scoring_lock'\)/);
  });
});
