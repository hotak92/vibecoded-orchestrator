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
// comments are dropped first, so a comment that merely mentions `<MenuBar />`
// can never satisfy the check.
//
// NOT A SANITISER, and it must never be pressed into service as one. The
// input is component source read off disk by a test; the output is SEARCHED
// as text (`indexOf`, `<Tag\b`) and is never assigned to `innerHTML`, never
// passed to `{@html}`, never handed to a DOM at all — the test environment
// has no DOM. Nothing here makes untrusted markup safe to render; for that
// the answer is `textContent`, or Svelte's default `{expr}` escaping.
//
// It is a SCANNER rather than a set of `.replace(/<script…/)` patterns, and
// that is deliberate. A regex denylist over markup cannot be made complete:
// `</script >` closes a script element and `/<script[\s\S]*?<\/script>/` does
// not match it (CodeQL js/bad-tag-filter), `<scr<script>ipt>` survives a
// single pass, and each patch to the pattern invites the next. Walking the
// source once and skipping the regions the HTML tokenizer would skip has no
// such gap — and it is what the paragraph above actually promises.

export type ShellPlacement = 'above-header' | 'below-header' | 'absent';

/** Elements whose content is TEXT, not markup, until their own end tag. */
const RAW_TEXT_ELEMENTS = ['script', 'style'] as const;

/** The whitespace the HTML tokenizer recognises around a tag name. */
function isTagWhitespace(ch: string | undefined): boolean {
  return ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r' || ch === '\f';
}

/** True when a tag name ENDS at `after` rather than continuing (`<scriptish`). */
function nameEndsAt(source: string, after: number): boolean {
  const next = source[after];
  return next === undefined || isTagWhitespace(next) || next === '>' || next === '/';
}

/**
 * Index just past the `>` that closes the tag being read from `i`, or the end
 * of the source when it is never closed. Quoted attribute values are honoured,
 * so `<script data-x=">">` ends at the SECOND `>`.
 */
function endOfTag(source: string, i: number): number {
  let quote = '';
  for (let j = i; j < source.length; j += 1) {
    const ch = source[j];
    if (quote !== '') {
      if (ch === quote) quote = '';
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '>') {
      return j + 1;
    }
  }
  return source.length;
}

/**
 * The raw-text element opening at `lt` (where `source[lt]` is `<`), or null.
 * `contentStart` is the first index of that element's content.
 */
function rawTextOpenAt(
  source: string,
  lt: number,
): { name: string; contentStart: number } | null {
  for (const name of RAW_TEXT_ELEMENTS) {
    const after = lt + 1 + name.length;
    if (source.slice(lt + 1, after).toLowerCase() !== name) continue;
    if (!nameEndsAt(source, after)) continue;
    return { name, contentStart: endOfTag(source, after) };
  }
  return null;
}

/**
 * Index just past the end tag of raw-text element `name`, searching from
 * `from`. An element that is never closed swallows the rest of the source:
 * for a placement check that is the safe direction — content that MIGHT be
 * script can never be read as template.
 */
function endOfRawText(source: string, name: string, from: number): number {
  const lower = source.toLowerCase();
  const opener = `</${name}`;
  let i = from;
  for (;;) {
    const close = lower.indexOf(opener, i);
    if (close < 0) return source.length;
    const after = close + opener.length;
    // `</script>`, `</script >`, `</script\n>` and `</script foo>` all end the
    // element — the tokenizer only requires the NAME to end there. `</scriptx`
    // does not, so the search continues past it.
    if (nameEndsAt(source, after)) return endOfTag(source, after);
    i = close + 2;
  }
}

/**
 * Svelte source with `<script>` / `<style>` elements and `<!-- -->` comments
 * removed, leaving the template markup. Imports (`import X from …`) live in
 * the script element and therefore cannot be mistaken for a mount.
 */
export function templateOf(svelteSource: string): string {
  let template = '';
  let i = 0;
  while (i < svelteSource.length) {
    const lt = svelteSource.indexOf('<', i);
    if (lt < 0) {
      template += svelteSource.slice(i);
      break;
    }
    template += svelteSource.slice(i, lt);
    if (svelteSource.startsWith('<!--', lt)) {
      const end = svelteSource.indexOf('-->', lt + 4);
      i = end < 0 ? svelteSource.length : end + 3;
      continue;
    }
    const raw = rawTextOpenAt(svelteSource, lt);
    if (raw !== null) {
      i = endOfRawText(svelteSource, raw.name, raw.contentStart);
      continue;
    }
    // An ordinary `<`: a component mount, an element, or `{#if a < b}`.
    template += '<';
    i = lt + 1;
  }
  return template;
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
