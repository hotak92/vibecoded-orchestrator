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
    header,
    body,
    footer,
  }: {
    open?: boolean;
    onClose?: () => void;
    closeOnBackdrop?: boolean;
    closeOnEscape?: boolean;
    width?: string;
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
  $effect(() => {
    if (!dialogEl) return;
    const wantOpen = open;
    const isOpen = dialogEl.open;
    if (wantOpen && !isOpen) {
      try { dialogEl.showModal(); } catch { /* already open */ }
    } else if (!wantOpen && isOpen) {
      dialogEl.close();
    } else if (wantOpen && isOpen && prevOpen === false) {
      // Defensive: same-tick false→true toggle that the effect
      // missed. Cycle the top layer.
      try { dialogEl.close(); } catch {}
      try { dialogEl.showModal(); } catch {}
    }
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
