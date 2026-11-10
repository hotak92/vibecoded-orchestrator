// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.106: keep a global notification layer (the toast host) ABOVE every
// open modal.
//
// Field report: an error raised while a modal was open (a hand-to-vco
// failure) rendered BEHIND the modal's blurred backdrop. Every launcher modal
// is a native `<dialog>` opened with `showModal()` (`DialogRoot.svelte`),
// which puts it in the browser TOP LAYER: no z-index outside the top layer
// can ever paint above it, and everything outside the dialog is inert (the
// toast's own × could not be clicked). A z-index bump cannot fix that.
//
// The one fix that works for every modal at once: while any `<dialog>` is
// open, the host element lives INSIDE the most recently opened one. It is
// then part of that dialog's top-layer entry (painted above its backdrop and
// content) and not inert. When the dialog closes or is removed, the host goes
// back to `<body>` — including when the dialog is torn out of the DOM with
// the host still inside it (the host is re-attached on the next mutation
// tick; its children are intact because a detached node keeps them).
//
// Used as a Svelte action (`use:topLayerHost`) on a SINGLE-ROOT component's
// root element (Svelte removes a single-root component by its one node,
// wherever that node currently lives).

/** Track open `<dialog>`s in the order they opened, and keep `host` in the
 *  topmost one (or in `<body>` when none is open). Returns a disposer. */
export function followTopLayer(host: HTMLElement, doc: Document = document): () => void {
  const stack: Element[] = [];

  const sync = () => {
    for (let i = stack.length - 1; i >= 0; i--) {
      const d = stack[i];
      if (!d.isConnected || !d.hasAttribute('open')) stack.splice(i, 1);
    }
    // Dialogs that opened since the last tick go on top, in DOM order.
    for (const d of Array.from(doc.querySelectorAll('dialog[open]'))) {
      if (!stack.includes(d)) stack.push(d);
    }
    const target: Element = stack[stack.length - 1] ?? doc.body;
    if (host.parentNode !== target) target.appendChild(host);
  };

  const observer = new MutationObserver(sync);
  observer.observe(doc.body, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['open'],
  });
  sync();
  return () => observer.disconnect();
}

/** Svelte action form of {@link followTopLayer}. */
export function topLayerHost(node: HTMLElement): { destroy: () => void } {
  const stop = followTopLayer(node);
  return {
    destroy() {
      stop();
      // The host may sit outside the component's original parent; take it
      // out explicitly so an unmounted component leaves nothing behind.
      node.remove();
    },
  };
}
