<script lang="ts">
  // DialogRoot — native <dialog>-based modal wrapper.
  //
  // Avoids the fixed-position+inset:0 modal pattern, which z-fought
  // with Tauri's GTK title bar in earlier builds. The native <dialog>
  // element renders in the browser top layer (above ALL other elements
  // including OS chrome inside the WebView), with native ::backdrop,
  // native escape-to-close, native accessibility tree treatment, and
  // native focus trapping when opened with showModal(). No CSS
  // centering math, no z-index, no position:fixed.
  //
  // Usage:
  //   <DialogRoot bind:this={dlg} onClose={...}>
  //     {#snippet header()}<h2>Title</h2>{/snippet}
  //     {#snippet body()}…{/snippet}
  //     {#snippet footer()}…{/snippet}
  //   </DialogRoot>
  //   dlg.open();   // calls showModal()
  //   dlg.close();
  //
  // Or drive open/close via the bindable `open` prop.

  import { onDestroy, onMount } from 'svelte';
  import type { Snippet } from 'svelte';

  let {
    open = $bindable<boolean>(false),
    onClose,
    closeOnBackdrop = true,
    closeOnEscape = true,
    width = '560px',
    ariaLabelledBy,
    ariaLabel,
    header,
    body,
    footer,
  }: {
    open?: boolean;
    onClose?: () => void;
    closeOnBackdrop?: boolean;
    closeOnEscape?: boolean;
    width?: string;
    // v0.2.35 (a11y sweep, Agent O): allow callers to wire the dialog's
    // accessible name. WCAG 2.4.6 — interactive components need an
    // accessible name; for native <dialog>, that's normally the first
    // heading in the dialog, but assistive tech only reliably picks it
    // up via aria-labelledby (or aria-label as a fallback). Without it,
    // some screen readers announce "dialog" with no title context on
    // open.
    ariaLabelledBy?: string;
    ariaLabel?: string;
    header?: Snippet;
    body?: Snippet;
    footer?: Snippet;
  } = $props();

  let dialogEl = $state<HTMLDialogElement | undefined>(undefined);

  // Sync external `open` prop → showModal/close. Also emit onClose when
  // the dialog closes via any path (Escape, backdrop click, .close()).
  //
  // 2026-04-28 (Bug B): if a caller's reactive state flips this prop
  // false-then-true within a single microtask (Svelte's effect
  // batching collapses to the final value `true`), the effect would
  // see `open=true` AND `dialogEl.open=true` (never actually closed
  // between flips) and skip the showModal() call — leaving the
  // <dialog> in whatever top-layer state it had, which under
  // WebKitGTK can mean an orphaned slot that captures clicks
  // viewport-wide on subsequent screens. We track the previous value
  // and force a close()+showModal() cycle when we observe such a
  // toggle, which guarantees the top layer is released and re-
  // acquired cleanly.
  let prevOpen = false;
  // 2026-05-26 (fork-bomb fix Windows/WebView2): re-entry guard.
  //
  // Symptom on Windows: launching the freshly-built launcher with the
  // OnboardingWizard auto-opening produced visible cascading windows so
  // fast the user couldn't screenshot. Single Tauri process; the
  // multiplication was the native <dialog> top-layer being torn down +
  // re-created in a microtask loop.
  //
  // Root cause: this $effect reads `dialogEl.open`, which is a reactive
  // property of the DOM element. On WebView2 (Chromium-based) the
  // dialog's onclose handler fires synchronously during the showModal()
  // call's microtask flush in certain race patterns (when the modal is
  // mounted with open=true initial AND the consumer also has its own
  // $effect chain feeding into `open`). The onclose handler sets
  // `open = false`, which re-triggers this $effect, which sees
  // wantOpen=true (the consumer's reactive `open` value at the *new*
  // microtask) AND isOpen=false (we just closed) AND prevOpen=true and
  // takes the third branch — close()+showModal() — which fires onclose
  // again, etc. Infinite loop, each iteration painting a new dialog
  // frame, hence "milioni di finestre" (a contributor's testing, 2026-05-25).
  //
  // The guard: a non-reactive `inFlight` flag that suppresses re-entry
  // while we're inside a showModal/close transition. Critically, we
  // also OMIT the "defensive third branch" entirely — same-tick
  // false→true toggles are handled by Svelte's effect batching
  // (which collapses to the final value); the third branch was a
  // workaround for a WebKitGTK Linux quirk (Bug B 2026-04-28) that
  // does not apply on Windows WebView2, and on Windows it actively
  // CAUSES the loop. We keep the guard cross-OS because re-entry is
  // never desirable; on Linux this is a no-op vs the prior behavior
  // (the false→true microtask path Bug B targeted no longer takes
  // the third branch — it takes the first branch on a subsequent tick
  // once Svelte's effect flush settles).
  let inFlight = false;
  $effect(() => {
    if (!dialogEl) return;
    if (inFlight) return;
    const wantOpen = open;
    const isOpen = dialogEl.open;
    if (wantOpen && !isOpen) {
      inFlight = true;
      try { dialogEl.showModal(); } catch { /* already open */ }
      inFlight = false;
    } else if (!wantOpen && isOpen) {
      inFlight = true;
      dialogEl.close();
      inFlight = false;
    }
    // No third branch: see the long comment above. wantOpen===isOpen
    // means there is nothing to do, and re-entering the close()+
    // showModal() cycle is what triggers the Windows fork-bomb.
    prevOpen = wantOpen;
  });

  function handleClose() {
    open = false;
    onClose?.();
  }

  /**
   * Native <dialog> Esc dispatches a 'cancel' event then 'close'. The
   * close event fires whether the user pressed Esc, called .close(), or
   * (in our case) clicked the backdrop. Single source of truth.
   */
  function onDialogClose() {
    if (open) handleClose();
  }

  /**
   * The browser doesn't expose ::backdrop click events directly. The
   * trick is: a click on the dialog element itself (not its children)
   * lands on the backdrop region, since the actual content lives inside
   * .dialog-content. Compare the event target to the dialog node.
   */
  function onDialogClick(e: MouseEvent) {
    if (!closeOnBackdrop || !dialogEl) return;
    if (e.target === dialogEl) {
      dialogEl.close();
    }
  }

  function onCancel(e: Event) {
    if (!closeOnEscape) {
      e.preventDefault();
    }
  }

  // Imperative API for callers that prefer .open() / .close() over the
  // bindable prop. Both work.
  export function showModal() {
    open = true;
  }
  export function close() {
    open = false;
  }

  onMount(() => {
    // If the consumer mounted with open=true, the $effect above already
    // handled the initial showModal call. Nothing else to do here.
  });

  // Defense in depth — release the native top-layer slot BEFORE Svelte
  // detaches the <dialog> node from the DOM.
  //
  // Background (2026-04-28 wizard bug): consumer code that wraps
  //   {#if flag}<DialogRoot open={true} ...>{/if}
  // unmounts the DialogRoot when `flag` flips to false. WebKitGTK keeps
  // the <dialog>'s top-layer entry alive UNTIL someone calls .close()
  // on the element — DOM removal alone is not enough to release the
  // top layer. The orphaned top-layer entry then captures pointer
  // events viewport-wide on subsequent screens (Bugs 1, 3, 4, 5 in the
  // OnboardingWizard). Per MDN: top-layer placement is set by
  // showModal() and released by close(), independent of DOM tree.
  //
  // Calling close() in onDestroy guarantees correct teardown even when
  // a (badly-written) caller passed `open={true}` as a literal and
  // then yanks the component via {#if} — the $effect above never sees
  // the open=true→false transition in that pattern, so it cannot do
  // this cleanup itself.
  onDestroy(() => {
    if (dialogEl?.open) {
      try { dialogEl.close(); } catch { /* already closed */ }
    }
  });
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<dialog
  bind:this={dialogEl}
  onclose={onDialogClose}
  oncancel={onCancel}
  onclick={onDialogClick}
  style:--dialog-width={width}
  aria-labelledby={ariaLabelledBy}
  aria-label={ariaLabel}
>
  <div class="dialog-content" role="document">
    {#if header}
      <div class="dialog-header">{@render header()}</div>
    {/if}
    {#if body}
      <div class="dialog-body">{@render body()}</div>
    {/if}
    {#if footer}
      <div class="dialog-footer">{@render footer()}</div>
    {/if}
  </div>
</dialog>

<style>
  /* Native <dialog> + ::backdrop. The browser handles centering and
     top-layer placement automatically when opened via showModal(). */
  dialog {
    border: none;
    padding: 0;
    margin: auto;            /* native centering for top-layer */
    background: transparent; /* .dialog-content paints its own bg */
    color: inherit;
    width: var(--dialog-width, 560px);
    max-width: min(92vw, 720px);
    max-height: calc(100vh - 4rem);
    overflow: visible;
    /* Override UA-default outline glow that some WebKit builds add. */
    outline: none;
  }
  dialog::backdrop {
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
  }

  /* Content shell: paints the visible card. Flex column so the body can
     scroll independently while header/footer stay pinned. */
  .dialog-content {
    display: flex;
    flex-direction: column;
    max-height: calc(100vh - 4rem);
    background: rgba(13, 23, 53, 0.97);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 14px;
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6);
    overflow: hidden;
    color: var(--color-text, #ddd);
  }
  .dialog-header {
    flex-shrink: 0;
    padding: 16px 20px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }
  .dialog-body {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 16px 20px;
  }
  .dialog-footer {
    flex-shrink: 0;
    padding: 12px 20px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }

  /* Optional fade-in. Native <dialog> animations are scoped to the
     element + its backdrop pseudo. */
  dialog[open] {
    animation: dialog-fade-in 0.15s ease-out;
  }
  dialog[open]::backdrop {
    animation: backdrop-fade-in 0.15s ease-out;
  }
  @keyframes dialog-fade-in {
    from { opacity: 0; transform: translateY(-6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  @keyframes backdrop-fade-in {
    from { opacity: 0; }
    to   { opacity: 1; }
  }
</style>
