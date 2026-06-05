<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.47: progress modal for codegraph-extras analyze operations.
  //
  // Reused for three flows from ExtraCodegraphPathsPanel:
  //   1. Sync after add  — title "Syncing <basename> into <project> codegraph"
  //   2. Reindex after remove or disable — title "Re-syncing <project> codegraph after path removal"
  //   3. Reindex after enable           — title "Re-syncing after re-enabling path"
  //
  // Behaviour per spec §14.4:
  //   - Indeterminate spinner (analyzer doesn't emit progress today).
  //   - "Hide" button minimises to a top-right status pill that re-opens
  //     the modal on click. State (running / failed / succeeded) is
  //     mirrored in the pill so the user can see at a glance.
  //   - On success: modal closes itself + the parent shows a toast.
  //   - On failure: modal stays open with the error string + "Retry" /
  //     "Close" buttons.
  //
  // The modal does NOT run the Tauri command itself — the parent panel
  // owns the promise and updates props. This keeps retry semantics in
  // the panel where they belong (the parent decides whether to call
  // syncExtraPath vs reindexAfterExtrasChange on retry).

  import DialogRoot from '$lib/components/DialogRoot.svelte';

  export type SyncModalState = 'running' | 'succeeded' | 'failed';

  let {
    open = $bindable<boolean>(true),
    /** Visible to assistive tech + the modal header. */
    title,
    /** Plain-text body line under the header (no markup). */
    bodyText,
    /** Current operation state, owned by the parent. Named `phase` to
     *  avoid the Svelte 5 `$state` rune name collision. */
    phase,
    /** Set when phase === 'failed'. */
    errorMessage = null,
    /** Click handler for "Retry" (only shown when phase === 'failed'). */
    onRetry,
    /** Click handler for "Close" + the minimised pill close. */
    onClose,
  }: {
    open?: boolean;
    title: string;
    bodyText: string;
    phase: SyncModalState;
    errorMessage?: string | null;
    onRetry?: () => void;
    onClose: () => void;
  } = $props();

  // Minimised state — when true the modal closes itself and the parent
  // is expected to render the status pill (we expose it via the same
  // event the user-clicked-Close path uses; the parent disambiguates
  // via the `hidden` second argument).
  let minimised = $state(false);

  function hide() {
    minimised = true;
    open = false;
  }

  /** Re-open after the user clicks the status pill. */
  export function reopen() {
    minimised = false;
    open = true;
  }

  function handleClose() {
    if (minimised) return;
    onClose();
  }

  function handleRetry() {
    onRetry?.();
  }
</script>

<DialogRoot
  bind:open
  closeOnBackdrop={false}
  closeOnEscape={phase !== 'running'}
  width="560px"
  ariaLabel={title}
  onClose={handleClose}
>
  {#snippet header()}
    <h2 class="extras-sync-title">{title}</h2>
  {/snippet}

  {#snippet body()}
    <div class="extras-sync-body">
      <p class="extras-sync-text">{bodyText}</p>

      {#if phase === 'running'}
        <div
          class="extras-sync-spinner"
          role="status"
          aria-live="polite"
          aria-label="Analyzing in progress"
        >
          <div class="extras-sync-spinner-ring"></div>
          <span class="extras-sync-spinner-label">Analyzing...</span>
        </div>
        <p class="extras-sync-hint">
          This may take a few minutes for large repos. You can hide
          this dialog and keep working; the sync runs in the
          background.
        </p>
      {:else if phase === 'failed'}
        <div class="extras-sync-error" role="alert">
          <strong>Analyze failed.</strong>
          <p>{errorMessage ?? 'Unknown error.'}</p>
        </div>
      {:else if phase === 'succeeded'}
        <p class="extras-sync-done">Done.</p>
      {/if}
    </div>
  {/snippet}

  {#snippet footer()}
    <div class="extras-sync-actions">
      {#if phase === 'running'}
        <button
          type="button"
          class="ps-btn-secondary"
          onclick={hide}
          aria-label="Hide modal and continue in the background"
        >
          Hide
        </button>
      {:else if phase === 'failed'}
        {#if onRetry}
          <button
            type="button"
            class="ps-btn-primary"
            onclick={handleRetry}
          >
            Retry
          </button>
        {/if}
        <button
          type="button"
          class="ps-btn-secondary"
          onclick={() => {
            open = false;
            onClose();
          }}
        >
          Close
        </button>
      {:else if phase === 'succeeded'}
        <button
          type="button"
          class="ps-btn-secondary"
          onclick={() => {
            open = false;
            onClose();
          }}
        >
          Close
        </button>
      {/if}
    </div>
  {/snippet}
</DialogRoot>

<style>
  .extras-sync-title {
    margin: 0;
    font-size: 14px;
    color: #c4b3ff;
  }
  .extras-sync-body {
    font-size: 13px;
    color: #ddd;
    line-height: 1.5;
  }
  .extras-sync-text {
    margin: 0 0 14px;
  }
  .extras-sync-hint {
    margin: 12px 0 0;
    font-size: 12px;
    color: #999;
  }
  .extras-sync-done {
    margin: 0;
    color: #0fc;
  }
  .extras-sync-error {
    margin: 8px 0 0;
    padding: 10px 12px;
    background: rgba(255, 99, 99, 0.08);
    border-left: 3px solid rgba(255, 99, 99, 0.5);
    border-radius: 4px;
    color: #ffb4b4;
  }
  .extras-sync-error p {
    margin: 4px 0 0;
    font-size: 12px;
    word-break: break-word;
  }
  .extras-sync-spinner {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 0;
  }
  .extras-sync-spinner-ring {
    width: 16px;
    height: 16px;
    border: 2px solid rgba(255, 255, 255, 0.15);
    border-top-color: rgb(0, 191, 166);
    border-radius: 50%;
    animation: extras-sync-spin 0.8s linear infinite;
  }
  .extras-sync-spinner-label {
    font-size: 12px;
    color: #999;
  }
  @keyframes extras-sync-spin {
    to {
      transform: rotate(360deg);
    }
  }
  .extras-sync-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }

  /* Local button styles — mirror IdentityTab patterns. */
  .ps-btn-primary {
    background: rgb(0, 191, 166);
    border: none;
    color: #000;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }
  .ps-btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .ps-btn-secondary {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .ps-btn-secondary:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
  }
  .ps-btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
