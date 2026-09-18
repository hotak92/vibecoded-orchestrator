// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.95 R7 — the setup progress banner renders BELOW the header.
//
// Field report (repeat): during post-adopt setup the launcher showed
// "Setting up <project>… Installing project bundle…" in a bar pinned to the
// window titlebar, above all other chrome. The cause was the mount order in
// the shell — `<ProjectSetupBanner />` before `<MenuBar />`, and `.menu-bar`
// is the window's drag region, so anything before it reads as titlebar.
//
// The launcher's vitest run is a `node` environment (launcher/vitest.config.ts
// — no jsdom, no component rendering), so these assert the shell's TEMPLATE
// via `shell-banner-placement.ts`, which strips script/style/comments first:
// a comment naming `<MenuBar />` cannot satisfy any check below (pinned by
// the fixtures in the first describe block).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  indexOfMount,
  placementRelativeToHeader,
  templateOf,
} from './shell-banner-placement';

const here = fileURLToPath(new URL('.', import.meta.url));

function read(relative: string): string {
  return readFileSync(`${here}/${relative}`, 'utf-8');
}

const LAYOUT = read('../routes/+layout.svelte');

describe('placement classifier (red-proof fixtures)', () => {
  // The defect shape, verbatim in miniature.
  const PRE_FIX = `
<div class="app-shell">
  <!-- this comment mentions <MenuBar /> and must not count -->
  <ProjectSetupBanner />
  <MenuBar />
  <div class="app-body"></div>
</div>`;

  const POST_FIX = `
<div class="app-shell">
  <MenuBar />
  <div class="shell-banners">
    <ProjectSetupBanner />
  </div>
  <div class="app-body"></div>
</div>`;

  it('calls the old arrangement above-header', () => {
    expect(placementRelativeToHeader(PRE_FIX, 'ProjectSetupBanner')).toBe('above-header');
  });

  it('calls the new arrangement below-header', () => {
    expect(placementRelativeToHeader(POST_FIX, 'ProjectSetupBanner')).toBe('below-header');
  });

  it('is not satisfied by a mention inside a comment or an import', () => {
    const commentOnly = `
<script lang="ts">
  import ProjectSetupBanner from '$lib/components/ProjectSetupBanner.svelte';
</script>
<div class="app-shell">
  <!-- <ProjectSetupBanner /> used to live here -->
  <MenuBar />
</div>`;
    expect(placementRelativeToHeader(commentOnly, 'ProjectSetupBanner')).toBe('absent');
    expect(templateOf(commentOnly)).not.toContain('ProjectSetupBanner');
  });

  it('does not confuse a tag with one that merely starts the same', () => {
    const other = `<MenuBar /><ProjectSetupBannerLegacy />`;
    expect(placementRelativeToHeader(other, 'ProjectSetupBanner')).toBe('absent');
  });
});

describe('the real shell', () => {
  it('mounts the setup banner below the header, not on the titlebar', () => {
    expect(placementRelativeToHeader(LAYOUT, 'ProjectSetupBanner')).toBe('below-header');
  });

  it('puts it inside the below-header banner stack', () => {
    const template = templateOf(LAYOUT);
    const stack = template.indexOf('class="shell-banners"');
    const banner = indexOfMount(template, 'ProjectSetupBanner');
    const body = template.indexOf('class="app-body"');
    expect(stack).toBeGreaterThan(-1);
    expect(banner).toBeGreaterThan(stack);
    expect(body).toBeGreaterThan(banner);
  });

  it('stacks banners in flow instead of overlaying them', () => {
    // A column stack that cannot be squeezed: two banners present at once
    // sit one under the other.
    expect(LAYOUT).toMatch(/\.shell-banners\s*\{[^}]*flex-direction:\s*column/);
    expect(LAYOUT).toMatch(/\.shell-banners\s*\{[^}]*flex-shrink:\s*0/);
    // And the banner itself is a normal-flow block — `position: absolute`
    // or `fixed` is exactly what would make two banners overlap.
    const shell = read('./components/StatusBannerShell.svelte');
    expect(shell).toMatch(/\.bg-banner\s*\{[^}]*display:\s*block/);
    expect(shell).not.toMatch(/\.bg-banner\s*\{[^}]*position:\s*(absolute|fixed)/);
  });
});

describe('one banner shell, no re-inlined clones', () => {
  const FAMILY = [
    'KgSyncBanner.svelte',
    'KgSummaryBanner.svelte',
    'CodeGraphBuildBanner.svelte',
    'OperationProgressBanner.svelte',
  ];

  it('every banner renders through StatusBannerShell', () => {
    for (const name of FAMILY) {
      const s = read(`./components/${name}`);
      expect(s, name).toMatch(/^\s*import\s+StatusBannerShell\s+from/m);
      expect(s, name).toContain('<StatusBannerShell');
    }
  });

  it('no banner carries its own copy of the shell chrome', () => {
    for (const name of FAMILY) {
      // Comments are stripped, so the prose "cloned verbatim from …" that
      // documents the history cannot mask a real re-inlined rule.
      const css = read(`./components/${name}`).replace(/\/\*[\s\S]*?\*\//g, '');
      expect(css, name).not.toMatch(/\.bg-banner\s*\{/);
      expect(css, name).not.toMatch(/\.bg-row\s*\{/);
      expect(css, name).not.toMatch(/\.bg-btn-primary\s*\{/);
    }
  });
});
