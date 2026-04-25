<script lang="ts">
  // Update notification badge for the MenuBar.
  //
  // Polls the orchestrator store on mount (which calls `check_for_updates`),
  // then shows a small dot + popover with an Update button when an update
  // is available. Persists "seen version" to localStorage so the toast
  // doesn't re-fire on every render — only when the underlying version
  // actually changes.

  import { onMount } from 'svelte';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { updater } from '$lib/stores/updater';

  let popoverOpen = $state(false);
  let wrapperEl = $state<HTMLDivElement | null>(null);

  const orchState = $derived($orchestrator);
  const upd = $derived($updater);

  $effect(() => {
    // Re-evaluate when the orchestrator store reports a change.
    void orchState.updateAvailable;
    void orchState.version;
    void orchState.status;
    updater.syncFromOrchestrator();
  });

  onMount(() => {
    // checkStatus() in the orchestrator store calls check_for_updates as
    // a side effect when an install is detected — we don't need to call
    // it ourselves. Just push current state into our store.
    updater.syncFromOrchestrator();
  });

  function handleClickOutside(e: MouseEvent) {
    if (popoverOpen && wrapperEl && !wrapperEl.contains(e.target as Node)) {
      popoverOpen = false;
    }
  }

  async function handleUpdate() {
    popoverOpen = false;
    await updater.runUpdate();
  }

  function handleDismiss() {
    popoverOpen = false;
    updater.dismiss();
  }

  // Don't render anything if no update or already dismissed for this version.
  const visible = $derived(upd.available && !upd.dismissed);
</script>

<svelte:window onclick={handleClickOutside} />

{#if visible}
  <div class="update-wrapper" bind:this={wrapperEl}>
    <button
      class="update-trigger"
      class:updating={upd.updating}
      onclick={(e) => { e.stopPropagation(); popoverOpen = !popoverOpen; }}
      title="Update available"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="23 4 23 10 17 10"/>
        <polyline points="1 20 1 14 7 14"/>
        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
      </svg>
      <span class="update-dot"></span>
    </button>

    {#if popoverOpen}
      <div class="update-popover">
        <div class="popover-header">
          <span class="popover-title">Update available</span>
        </div>
        <p class="popover-desc">
          A new version of the orchestrator is available. Current: <span class="mono">{orchState.version || 'unknown'}</span>
        </p>
        {#if upd.error}
          <div class="popover-error">{upd.error}</div>
        {/if}
        <div class="popover-actions">
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={handleDismiss} disabled={upd.updating}>
            Later
          </button>
          <button class="btn-3d btn-3d-primary btn-3d-sm" onclick={handleUpdate} disabled={upd.updating}>
            {#if upd.updating}
              Updating…
            {:else}
              Update
            {/if}
          </button>
        </div>
      </div>
    {/if}
  </div>
{/if}

<style>
  .update-wrapper {
    position: relative;
  }

  .update-trigger {
    position: relative;
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(0, 191, 166, 0.3);
    border-radius: 10px;
    color: var(--color-teal);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .update-trigger:hover {
    background: rgba(0, 191, 166, 0.08);
    border-color: rgba(0, 191, 166, 0.5);
  }

  .update-trigger.updating svg {
    animation: spin 1.2s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .update-dot {
    position: absolute;
    top: 6px;
    right: 6px;
    width: 8px;
    height: 8px;
    background: var(--color-pink);
    border: 2px solid rgba(8, 15, 40, 1);
    border-radius: 50%;
  }

  .update-popover {
    position: fixed;
    top: 52px;
    right: 16px;
    width: 280px;
    background: rgba(13, 23, 53, 0.97);
    backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 14px;
    padding: 14px;
    z-index: 250;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    animation: pop-in 0.15s ease-out;
  }

  @keyframes pop-in {
    from { opacity: 0; transform: translateY(-8px) scale(0.96); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  .popover-header {
    margin-bottom: 6px;
  }

  .popover-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-text);
  }

  .popover-desc {
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
    margin-bottom: 12px;
  }

  .mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    color: var(--color-text);
  }

  .popover-error {
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    border-radius: 8px;
    color: var(--color-pink);
    font-size: 11px;
    margin-bottom: 10px;
  }

  .popover-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
