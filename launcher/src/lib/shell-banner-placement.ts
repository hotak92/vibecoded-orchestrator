// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.95 R7 — where a component sits in the app shell, as a decidable fact.
//
// The defect this closes was pure PLACEMENT: the project-setup progress
// banner was mounted before `<MenuBar />`, so it rendered at the very top
// edge of the window — a strip glued to the titlebar (`.menu-bar` carries
// `-webkit-app-region: drag`, i.e. it IS the draggable title area), above
// all other chrome and visually detached from the content it describes.
//
// The launcher's vitest run is a `node` environment (no jsdom, no
// @testing-library/svelte — see launcher/vitest.config.ts), so the rendered
// DOM cannot be asserted. These helpers make the placement checkable from
// the component SOURCE instead, on the TEMPLATE only: script, style and
// comments are stripped first, so a comment that merely mentions `<MenuBar />`
// can never satisfy the check.

export type ShellPlacement = 'above-header' | 'below-header' | 'absent';

/**
 * Strip `<script>` / `<style>` blocks and `<!-- -->` comments from Svelte
 * source, leaving the template markup. Imports (`import X from …`) live in
 * the script block and therefore cannot be mistaken for a mount.
 */
export function templateOf(svelteSource: string): string {
  return svelteSource
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<!--[\s\S]*?-->/g, '');
}

/** Index of a component mount (`<Tag …`) in already-stripped template
 *  markup, or -1. Exported so callers can express their own ordering
 *  assertions (e.g. "inside the stack, before the body"). */
export function indexOfMount(template: string, tag: string): number {
  return template.search(new RegExp(`<${tag}\\b`));
}

/**
 * Classify where `tag` is mounted relative to the shell's header component.
 *
 * `above-header` is the defect shape: the window's top edge, level with the
 * titlebar. `below-header` is the remedy: inside the shell's banner stack,
 * under the chrome it belongs to.
 */
export function placementRelativeToHeader(
  svelteSource: string,
  tag: string,
  headerTag = 'MenuBar',
): ShellPlacement {
  const template = templateOf(svelteSource);
  const target = indexOfMount(template, tag);
  if (target < 0) return 'absent';
  const header = indexOfMount(template, headerTag);
  if (header < 0) return 'absent';
  return target < header ? 'above-header' : 'below-header';
}
