// dom-async.ts — small DOM-timing helpers shared across components.
//
// One home for "wait for the browser to actually do a thing" primitives so
// callers don't re-derive rAF/microtask plumbing (and so the timing contract
// is documented + unit-testable in one place).

/**
 * Resolve after the browser has painted at least one frame.
 *
 * Why double requestAnimationFrame: a single rAF callback runs BEFORE the
 * paint of the frame it was scheduled in; the second rAF, scheduled from
 * inside the first, runs after that paint has been committed. The pair is
 * the canonical "let the browser complete a real frame" barrier.
 *
 * This matters for native <dialog> teardown on Chromium-based webviews
 * (Windows WebView2): closing a modal <dialog> flips its `.open` JS property
 * synchronously, but Chromium releases the top-layer ::backdrop slot on a
 * later frame. A Svelte `await tick()` only flushes the microtask/effect
 * queue — it is NOT a barrier on the top-layer release. Awaiting nextFrame()
 * before unmounting the dialog's host element gives Chromium the frame it
 * needs to release the slot, preventing an orphaned backdrop that captures
 * pointer events viewport-wide (the v0.2.66 post-add navigation freeze).
 *
 * Combined with DialogRoot.svelte's unconditional onDestroy close(), this is
 * belt-and-suspenders: the unconditional close() is the structural fix, and
 * nextFrame() ensures the close() has a frame to take effect before unmount.
 *
 * In a non-DOM environment (SSR, tests without rAF) it falls back to a
 * macrotask via setTimeout(0) so callers can always await it safely.
 */
export function nextFrame(): Promise<void> {
  if (typeof requestAnimationFrame !== 'function') {
    return new Promise((resolve) => setTimeout(resolve, 0));
  }
  return new Promise((resolve) => {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => resolve());
    });
  });
}
