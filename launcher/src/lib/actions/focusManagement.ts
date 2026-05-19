// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared Svelte actions for modal/dialog focus management.
//
// Extracted in v0.2.18 from `launcher/src/routes/preferences/+page.svelte`
// (Commit 7 / AGENT-PREFS-UI landed both actions inline for the "Set
// OpenAI as default?" confirm modal). Lifting them to a reusable surface
// lets every modal in the launcher share the same a11y semantics — focus
// the primary action on mount, trap Tab/Shift+Tab inside the dialog —
// without duplicating the implementation.
//
// Signatures + behaviour are unchanged from the inline version; this
// file is a pure refactor (no a11y regressions, no Svelte-action API
// changes). Consumers import from `$lib/actions/focusManagement`.
//
// Svelte 5 actions take a node and return either void or
// `{ destroy(): void }`. We follow that contract here so the actions
// compose with Svelte 5's runes-based reactivity (`$state`/`$derived`).

/**
 * Focus the supplied button on mount.
 *
 * Used as: `<button use:focusOnMount>...</button>` inside a modal's
 * primary action slot so the keyboard user lands on the affirmative
 * button when the dialog opens.
 *
 * The microtask defer is load-bearing: Svelte 5 wires up the rest of
 * the modal in the same synchronous tick as the action callback, and
 * calling `.focus()` before sibling nodes exist can cause the focus to
 * silently land on `<body>` (browser-dependent — verified in webkit2gtk
 * Tauri webviews on Linux). Deferring to the next microtask guarantees
 * the full subtree is in the DOM.
 */
export function focusOnMount(node: HTMLButtonElement): void {
  // Defer to next tick so Svelte has wired up the rest of the modal.
  queueMicrotask(() => node.focus());
}

/**
 * Minimal focus trap: cycle Tab / Shift+Tab between the focusable
 * descendants of `node` so focus never escapes the dialog while it's
 * open.
 *
 * Used as: `<div role="dialog" aria-modal="true" use:focusTrap>...</div>`
 * on the modal root. The action attaches a keydown listener that
 * intercepts Tab at the boundaries (last → first, first → last with
 * shift); other keys propagate normally.
 *
 * Focusable-selector list matches the WAI-ARIA Authoring Practices'
 * "focusable elements" set, minus `iframe` (irrelevant here) and
 * `audio[controls]/video[controls]` (no media in our modals). If a
 * future modal adds a media element, extend the selector inline rather
 * than maintaining a parallel list — that's the only reason this lives
 * as a local `const` rather than a module-level export.
 */
export function focusTrap(
  node: HTMLElement,
): { destroy(): void } {
  function onKey(e: KeyboardEvent) {
    if (e.key !== 'Tab') return;
    const focusable = node.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (e.shiftKey && active === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }
  node.addEventListener('keydown', onKey);
  return {
    destroy() {
      node.removeEventListener('keydown', onKey);
    },
  };
}
