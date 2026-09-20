// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.95 R4 — the Remote Control endpoint gate is NAMED where the user hits it.
//
// The defect: while the panel is in Multimodel mode, `/remote-control` inside
// Claude Code always fails ("Remote Control initialization failed") because
// the client refuses it whenever ANTHROPIC_BASE_URL is not api.anthropic.com
// (>= 2.1.196). The launcher cannot observe that failure — it happens in
// another process — and everything it DID say about the gate lived in pill
// tooltips, which nobody reads before clicking and nobody sees afterwards.
//
// So the remedy is a standing line in the status bar, and what these tests
// pin is (a) when it is produced and what it says, (b) that the sentence has
// ONE home shared with the Services-page warning, and (c) that StatusBar
// actually RENDERS it rather than only importing it.
//
// The launcher's vitest run is a `node` environment (launcher/vitest.config.ts
// — no jsdom, no component renderer), so (c) is asserted on the component's
// TEMPLATE via `shell-banner-placement.ts`, which strips script, style and
// comments first: a mention inside the `<script>` import list or in a comment
// cannot satisfy it (red-proofed by the fixtures in the last describe block).

import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

vi.mock('$lib/tauri', () => ({ invoke: vi.fn() }));

import {
  MODE_PILL_TOOLTIP,
  REMOTE_CONTROL_EXITS,
  REMOTE_CONTROL_GATE,
  pointPanelWarnings,
  remoteControlGateNotice,
} from '$lib/api/model_gateway';
import type { PanelMode, PanelModeReport } from '$lib/api/model_gateway';
import type { VSCodeInspection } from '$lib/types/model-gateway';
import { indexOfMount, templateOf } from '$lib/shell-banner-placement';

const here = fileURLToPath(new URL('.', import.meta.url));
const STATUS_BAR = readFileSync(`${here}/StatusBar.svelte`, 'utf-8');

function report(mode: PanelMode, extra: Partial<PanelModeReport> = {}): PanelModeReport {
  return {
    action: 'panel_mode',
    path: '/home/u/.config/Code/User/settings.json',
    ok: true,
    mode,
    base_url: mode === 'multimodel' ? 'http://127.0.0.1:11436' : null,
    model: null,
    default_model_is_vendor: false,
    slot_overrides: [],
    gateway: 'running',
    endpoint: mode === 'multimodel' ? 'running' : null,
    prototype_endpoint: false,
    stash_present: false,
    ...extra,
  } as PanelModeReport;
}

function inspection(): VSCodeInspection {
  return {
    path: '/home/u/.config/Code/User/settings.json',
    exists: true,
    parseable: true,
    refusal_reason: null,
    message: null,
    points_at_vco_gateway: false,
    base_url: null,
    model: null,
    discovery_enabled: null,
    disable_login_prompt: null,
    slot_overrides: [],
    managed_keys_present: [],
    permissions: 'owner_only',
  };
}

describe('the gate sentence', () => {
  it('names the gate, the version and the host — the three facts a user needs', () => {
    expect(REMOTE_CONTROL_GATE).toContain('Remote Control is refused');
    expect(REMOTE_CONTROL_GATE).toContain('2.1.196');
    expect(REMOTE_CONTROL_GATE).toContain('api.anthropic.com');
  });

  it('names BOTH exits, and the detached server by the name it ships under', () => {
    expect(REMOTE_CONTROL_EXITS).toContain('Remote Control mode');
    expect(REMOTE_CONTROL_EXITS).toContain('rc-native');
    expect(REMOTE_CONTROL_EXITS).toContain('--remote-control');
  });

  it('promises no compatibility and suggests no downgrade', () => {
    const text = `${REMOTE_CONTROL_GATE} ${REMOTE_CONTROL_EXITS}`.toLowerCase();
    expect(text).not.toContain('downgrade');
    expect(text).not.toContain('will work');
    expect(text).not.toContain('compatible');
  });

  it('has ONE home: the Services-page warning is composed from it, not a copy', () => {
    const warning = pointPanelWarnings(inspection())[0];
    expect(warning).toContain(REMOTE_CONTROL_GATE);
    expect(warning).toContain(REMOTE_CONTROL_EXITS);
    // The pre-point surface keeps the extras its own tests pin.
    expect(warning).toContain('paste-ready block');
    expect(warning).toContain('docs/TROUBLESHOOTING.md');
  });
});

describe('when the notice is produced', () => {
  it('speaks in multimodel — the mode in which the gate actually bites', () => {
    const text = remoteControlGateNotice(report('multimodel'));
    expect(text).toContain(REMOTE_CONTROL_GATE);
    expect(text).toContain(REMOTE_CONTROL_EXITS);
  });

  it('says nothing once the panel is on Remote Control: the gate is gone', () => {
    expect(remoteControlGateNotice(report('remote-control'))).toBe('');
  });

  it('says nothing for a custom endpoint VCO leaves alone, or an unreadable file', () => {
    expect(remoteControlGateNotice(report('unmanaged'))).toBe('');
    expect(remoteControlGateNotice(report('unparseable'))).toBe('');
  });

  it('says nothing before the first probe resolves — it never guesses the mode', () => {
    expect(remoteControlGateNotice(null)).toBe('');
  });

  it('is more than the tooltip it replaces as the primary surface', () => {
    // The tooltip stays (it is the per-pill trade-off) but it names neither
    // the cause nor an exit, which is why it was not enough on its own.
    expect(MODE_PILL_TOOLTIP.multimodel).not.toContain('api.anthropic.com');
    expect(remoteControlGateNotice(report('multimodel'))).toContain('api.anthropic.com');
  });
});

describe('StatusBar renders it (template, not just import)', () => {
  const template = templateOf(STATUS_BAR);

  it('renders the notice text in the markup, guarded by its own condition', () => {
    expect(template).toContain('{#if remoteControlGate}');
    expect(template).toContain('{remoteControlGate}');
  });

  it('places it inside the panel-mode group, beside the Remote Control pill', () => {
    // "the Remote Control affordance shows the sentence" is a placement
    // claim: after the pills, before the status bar's app-count tail.
    const pills = indexOfMount(template, 'select');
    const gate = template.indexOf('{#if remoteControlGate}');
    const tail = template.indexOf('activated');
    expect(gate).toBeGreaterThan(-1);
    expect(gate).toBeGreaterThan(pills);
    expect(gate).toBeLessThan(tail);
  });

  it('is not the pink warning tone — nothing is broken when it shows', () => {
    expect(template).toContain('class="mode-gate"');
    expect(template).not.toMatch(/class="mode-warning"[^>]*>\s*<span[^>]*>\{remoteControlGate\}/);
  });

  it('carries the full sentence in a title, since the bar truncates', () => {
    expect(template).toContain('title={remoteControlGate}');
  });

  it('the checks above cannot be satisfied by a comment or the import', () => {
    const decoy = `
<script lang="ts">
  import { remoteControlGateNotice } from '$lib/api/model_gateway';
  const remoteControlGate = $derived(remoteControlGateNotice(report));
</script>
<footer>
  <!-- {#if remoteControlGate}{remoteControlGate}{/if} used to be here -->
</footer>`;
    expect(templateOf(decoy)).not.toContain('remoteControlGate');
  });
});
