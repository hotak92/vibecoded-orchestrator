// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.91 decision #27 — the Hooks tab's presentation rules.
//
// The tab used to render one `enabled` boolean off a DB row nothing enforced.
// These tests pin the three-state model that replaced it, and in particular
// the cases where the honest answer is "this control does nothing, so it is
// off" rather than a checkbox that silently lies.

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  canToggle,
  detectHintOs,
  gitVisibilityNote,
  isChecked,
  leanCtxChoiceFromEnvValue,
  leanCtxEnvValueForChoice,
  leanCtxToastText,
  LEAN_CTX_HINT,
  LEAN_CTX_KEY,
  LEAN_CTX_OPTIONS,
  newHookCommandPlaceholder,
  parseTimeoutSeconds,
  registerBlockedReason,
  settingsErrorBanner,
  stateLabel,
  stateTooltip,
  timeoutSeconds,
  unregisterConfirmText,
  type EffectiveHook,
  type HookState,
} from './hooks-view';

const SETTINGS = '/home/u/proj/.claude/settings.json';

function hook(overrides: Partial<EffectiveHook> = {}): EffectiveHook {
  return {
    id: 1,
    event: 'PostToolUse',
    matcher: 'Edit(*)',
    command: 'bash .claude/hooks/post-file-edit.sh',
    source: 'bundled',
    source_module: null,
    timeout_ms: 30000,
    state: 'active',
    ...overrides,
  };
}

describe('checkbox state reflects enforcement, not a stored flag', () => {
  it('is checked only for a hook settings.json actually declares', () => {
    expect(isChecked(hook({ state: 'active' }))).toBe(true);
    expect(isChecked(hook({ state: 'disabled' }))).toBe(false);
    expect(isChecked(hook({ state: 'orphan' }))).toBe(false);
  });
});

describe('canToggle — never offer a control that cannot deliver', () => {
  it('allows toggling active and disabled hooks when the file is readable', () => {
    expect(canToggle(hook({ state: 'active' }), true)).toBe(true);
    expect(canToggle(hook({ state: 'disabled' }), true)).toBe(true);
  });

  it('refuses an orphan: nothing to remove, nothing parked to restore', () => {
    expect(canToggle(hook({ state: 'orphan' }), true)).toBe(false);
  });

  it('refuses everything when settings.json cannot be read', () => {
    for (const state of ['active', 'disabled', 'orphan'] as HookState[]) {
      expect(canToggle(hook({ state }), false)).toBe(false);
    }
  });
});

describe('state labels and tooltips are specific, not generic', () => {
  it('gives each state its own label', () => {
    const labels = (['active', 'disabled', 'orphan'] as HookState[]).map(stateLabel);
    expect(new Set(labels).size).toBe(3);
    expect(labels).toEqual(['Running', 'Disabled', 'Not in settings.json']);
  });

  it('names the settings file in every tooltip', () => {
    for (const state of ['active', 'disabled', 'orphan'] as HookState[]) {
      expect(stateTooltip(state, SETTINGS)).toContain(SETTINGS);
    }
  });

  it('promises exact restore for a disabled hook and says the script survives', () => {
    const t = stateTooltip('disabled', SETTINGS);
    expect(t).toMatch(/restores it exactly/);
    expect(t).toMatch(/script file was not touched/);
  });

  it('tells the orphan story honestly: it does not run and cannot be restored', () => {
    const t = stateTooltip('orphan', SETTINGS);
    expect(t).toMatch(/does not run/);
    expect(t).toMatch(/nothing stored to restore/);
  });
});

describe('settingsErrorBanner — a refusal the user can act on', () => {
  it('says nothing was written for the destructive-looking failures', () => {
    for (const code of ['unparseable', 'jsonc_edit_refused', 'hooks_block_malformed', 'no_python']) {
      expect(settingsErrorBanner(code, null, SETTINGS)).toMatch(/[Nn]othing was written/);
    }
  });

  it('points a missing settings.json at the bundle install', () => {
    expect(settingsErrorBanner('missing', null, SETTINGS)).toMatch(/Update bundle/);
  });

  it('explains WHY an unparseable file is not rewritten', () => {
    const t = settingsErrorBanner('unparseable', null, SETTINGS);
    expect(t).toMatch(/not valid JSON or JSONC/);
    expect(t).toMatch(/could destroy/);
  });

  it('explains a refused in-place JSONC edit (v0.2.97: JSONC is edited, not refused)', () => {
    const t = settingsErrorBanner('jsonc_edit_refused', null, SETTINGS);
    expect(t).toMatch(/comments or trailing commas/);
    expect(t).toMatch(/UPDATE_DEFERRED\.md/);
  });

  it('falls back to the backend message for an unknown code', () => {
    expect(settingsErrorBanner('brand_new_code', 'the disk caught fire', SETTINGS)).toBe(
      'the disk caught fire',
    );
  });

  it('still says something useful when there is no message at all', () => {
    expect(settingsErrorBanner(null, null, SETTINGS)).toContain(SETTINGS);
  });
});

describe('unregisterConfirmText — both facts before the click', () => {
  it('always states the script file is not deleted', () => {
    for (const state of ['active', 'disabled', 'orphan'] as HookState[]) {
      expect(unregisterConfirmText(hook({ state }), SETTINGS)).toMatch(
        /script file itself is NOT deleted/,
      );
    }
  });

  it('says the hook stops running when it is currently active', () => {
    expect(unregisterConfirmText(hook({ state: 'active' }), SETTINGS)).toMatch(
      /stops running/,
    );
  });

  it('does not claim a stop for a hook that was already not running', () => {
    const t = unregisterConfirmText(hook({ state: 'orphan' }), SETTINGS);
    expect(t).not.toMatch(/stops running/);
    expect(t).toMatch(/already absent/);
  });

  it('names the command so the user knows which row they clicked', () => {
    expect(unregisterConfirmText(hook(), SETTINGS)).toContain(
      'bash .claude/hooks/post-file-edit.sh',
    );
  });
});

describe('gitVisibilityNote', () => {
  it('names the file and warns it is usually git-tracked', () => {
    const note = gitVisibilityNote(SETTINGS);
    expect(note).toContain(SETTINGS);
    expect(note).toMatch(/git/);
  });
});

describe('timeoutSeconds — the DB stores ms, the UI shows seconds', () => {
  it('converts and rounds', () => {
    expect(timeoutSeconds(hook({ timeout_ms: 30000 }))).toBe(30);
    expect(timeoutSeconds(hook({ timeout_ms: 1500 }))).toBe(2);
  });

  it('passes null through rather than showing 0s', () => {
    expect(timeoutSeconds(hook({ timeout_ms: null }))).toBeNull();
  });
});

describe('parseTimeoutSeconds', () => {
  it('treats blank as "no timeout"', () => {
    expect(parseTimeoutSeconds('')).toEqual({ ok: true, value: null });
    expect(parseTimeoutSeconds('   ')).toEqual({ ok: true, value: null });
  });

  it('accepts a positive whole number', () => {
    expect(parseTimeoutSeconds(' 30 ')).toEqual({ ok: true, value: 30 });
  });

  it('rejects what the backend would reject, with the reason', () => {
    expect(parseTimeoutSeconds('0')).toEqual({
      ok: false,
      error: 'Timeout must be greater than zero.',
    });
    for (const bad of ['-5', '1.5', 'abc', '30s']) {
      const r = parseTimeoutSeconds(bad);
      expect(r.ok).toBe(false);
    }
  });
});

describe('registerBlockedReason', () => {
  it('passes a complete form', () => {
    expect(registerBlockedReason('Stop', 'bash .claude/hooks/x.sh', '')).toBeNull();
    expect(registerBlockedReason('Stop', 'bash .claude/hooks/x.sh', '10')).toBeNull();
  });

  it('blocks on a missing event or command', () => {
    expect(registerBlockedReason('', 'cmd', '')).toBe('Pick an event.');
    expect(registerBlockedReason('Stop', '   ', '')).toBe('Enter the command to run.');
  });

  it('surfaces the timeout error rather than letting it round-trip', () => {
    expect(registerBlockedReason('Stop', 'cmd', 'soon')).toMatch(/whole number/);
  });
});

describe('newHookCommandPlaceholder — the hint must fit the OS that reads it', () => {
  it('shows the bash form on Linux and macOS', () => {
    for (const ua of [
      'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    ]) {
      expect(detectHintOs(ua)).toBe('other');
      expect(newHookCommandPlaceholder(detectHintOs(ua))).toBe(
        'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/my-hook.sh"',
      );
    }
  });

  it('shows the exact powershell -File form on Windows — the form the compatibility doc says survives the PowerShell fallback', () => {
    expect(detectHintOs('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')).toBe(
      'windows',
    );
    expect(newHookCommandPlaceholder('windows')).toBe(
      'powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PROJECT_DIR}/.claude/hooks/my-hook.ps1"',
    );
  });

  it('never shows the bash form on Windows (a copied bash hint is a broken hook)', () => {
    expect(newHookCommandPlaceholder('windows')).not.toContain('bash');
  });
});


// ─── lean-ctx per-project toggle (v0.2.101: wired + accurate copy) ───────
//
// The v0.2.101 GUI audit found the PR-6 (v0.2.11) toggle had shipped its
// state + handlers in HooksTab.svelte with NO markup ever rendering them —
// a delivered-nowhere control. The mapping/copy now live in hooks-view and
// are pinned here; the wiring itself (markup + onChange) is asserted
// structurally below against the .svelte source.

describe('lean-ctx toggle — state mapping', () => {
  it('maps the two on-disk values the hook reads, and nothing else', () => {
    expect(leanCtxChoiceFromEnvValue('on')).toBe('on');
    expect(leanCtxChoiceFromEnvValue('off')).toBe('off');
    // absent key = the hook's own default ("on"), rendered as 'default'
    expect(leanCtxChoiceFromEnvValue(null)).toBe('default');
    expect(leanCtxChoiceFromEnvValue(undefined)).toBe('default');
    // a manual edit the hook would ignore keeps rendering as 'default' —
    // the on-disk value survives until the user actively moves the toggle
    expect(leanCtxChoiceFromEnvValue('OFF!')).toBe('default');
    expect(leanCtxChoiceFromEnvValue('')).toBe('default');
    // case-insensitive: BOTH hook siblings compare case-insensitively
    // (.sh via a POSIX case-glob, .ps1 via ToLowerInvariant — SF-3)
    expect(leanCtxChoiceFromEnvValue('Off')).toBe('off');
    expect(leanCtxChoiceFromEnvValue('ON')).toBe('on');
  });

  it('persists on/off and REMOVES the key for default (null)', () => {
    expect(leanCtxEnvValueForChoice('on')).toBe('on');
    expect(leanCtxEnvValueForChoice('off')).toBe('off');
    expect(leanCtxEnvValueForChoice('default')).toBeNull();
  });

  it('owns the VCO_LEAN_CTX_DEFAULT key the hooks read', () => {
    expect(LEAN_CTX_KEY).toBe('VCO_LEAN_CTX_DEFAULT');
  });

  it('offers exactly the three logical states', () => {
    expect(LEAN_CTX_OPTIONS.map((o) => o.value)).toEqual(['default', 'on', 'off']);
  });
});

describe('lean-ctx toggle — copy must match the allow-list rule the hooks enforce', () => {
  it('describes the v0.2.101 rule positively: allow-list, raw-by-default, lossless pointer', () => {
    expect(LEAN_CTX_HINT).toMatch(/allow-listed/i);
    expect(LEAN_CTX_HINT).toMatch(/run raw|runs raw|raw/i);
    expect(LEAN_CTX_HINT).toMatch(/pointer/i);
    expect(LEAN_CTX_HINT).toContain('.claude/state/lean-ctx-tee/');
  });

  it('never re-describes the retired compress-everything rule', () => {
    expect(LEAN_CTX_HINT).not.toMatch(/every Bash|all commands|compresses everything/i);
  });

  it('names the lean-ctx binary prerequisite (without it the hook no-ops)', () => {
    expect(LEAN_CTX_HINT).toMatch(/lean-ctx binary/i);
  });

  it('confirms after a write in the state the user picked', () => {
    expect(leanCtxToastText('default')).toMatch(/default/i);
    expect(leanCtxToastText('off')).toMatch(/off/);
    expect(leanCtxToastText('on')).toMatch(/\bon\b/);
  });
});

describe('lean-ctx toggle — HooksTab wiring (structural)', () => {
  // The control was delivered-nowhere once already (logic shipped, markup
  // never rendered, loader never called). These pins read the .svelte
  // source: a future refactor that drops the render or the load goes red
  // here instead of shipping a second placebo.
  const svelte = readFileSync(
    new URL('./HooksTab.svelte', import.meta.url),
    'utf-8',
  );

  it('renders the Dropdown with the shared options', () => {
    expect(svelte).toContain('LEAN_CTX_OPTIONS');
    expect(svelte).toMatch(/<Dropdown[^>]*options=\{LEAN_CTX_OPTIONS\}/s);
  });

  it('calls loadLeanCtx from the project effect (not dead state)', () => {
    expect(svelte).toMatch(/void loadLeanCtx\(\)/);
  });

  it('persists through setLeanCtx on change', () => {
    expect(svelte).toMatch(/onChange=\{\(v\) => void setLeanCtx\(v\)\}/);
    expect(svelte).toContain('set_claude_env_value');
  });

  it('shows the accurate hint text', () => {
    expect(svelte).toContain('LEAN_CTX_HINT');
  });
});
