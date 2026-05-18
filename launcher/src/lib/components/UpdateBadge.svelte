<script lang="ts">
  // Update notification badge for the MenuBar.
  //
  // Polls the orchestrator store on mount (which calls `check_for_updates`),
  // then shows a small dot + popover with an Update button when an update
  // is available. Persists "seen version" to localStorage so the toast
  // doesn't re-fire on every render — only when the underlying version
  // actually changes.
  //
  // v0.2.16 (W4 / 0.5): three-state banner. The Rust `check_for_updates`
  // command now returns a full UpdateStatus struct with three flags:
  //   - binary_stale  (highest priority) — newer launcher binary on disk
  //                   than the running process. Resolved by restart.
  //   - install_stale — source version > install manifest version
  //                     (user `git pull`-ed manually). Resolved by
  //                     install.py --update only (no git pull).
  //   - remote_ahead  — origin/main is ahead of local. Resolved by
  //                     full `update_orchestrator` (git pull + install).
  //
  // The banner renders ONE state at a time, in priority order.

  import { onMount } from 'svelte';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { updater } from '$lib/stores/updater';

  let popoverOpen = $state(false);
  let wrapperEl = $state<HTMLDivElement | null>(null);

  const orchState = $derived($orchestrator);
  const upd = $derived($updater);

  // v0.2.16 (W4 / 0.5): copy + action per kind. Drives both popover
  // header text and primary button label/handler.
  const kindCopy = $derived.by(() => {
    const us = orchState.updateStatus;
    switch (upd.kind) {
      case 'binary_stale':
        return {
          title: 'Restart Launcher',
          desc: us
            ? `A newer launcher binary is on disk (v${us.on_disk_binary_version}). The running launcher is v${us.running_version}. Restart to load it.`
            : 'A newer launcher binary is on disk. Restart to load it.',
          buttonLabel: 'Restart Launcher',
          actionKey: 'restart' as const,
        };
      case 'install_stale':
        return {
          title: 'Install Update',
          desc: us
            ? `v${us.source_version} is on disk; last install ran v${us.installed_version}. Click Install Update to apply.`
            : 'Source is newer than the last successful install. Click Install Update to apply.',
          buttonLabel: 'Install Update',
          actionKey: 'install' as const,
        };
      case 'remote_ahead':
        return {
          title: 'Update available',
          desc: us
            ? `A new version of the orchestrator is available on the remote. Current: v${us.installed_version || us.source_version || orchState.version || 'unknown'}.`
            : 'A new version of the orchestrator is available.',
          buttonLabel: 'Fetch + Install',
          actionKey: 'fetch_install' as const,
        };
      default:
        return {
          title: 'Up to date',
          desc: 'No pending updates detected.',
          buttonLabel: '',
          actionKey: null as null | 'restart' | 'install' | 'fetch_install',
        };
    }
  });

  $effect(() => {
    // Re-evaluate when the orchestrator store reports a change.
    void orchState.updateAvailable;
    void orchState.updateStatus;
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

  async function handleAction() {
    popoverOpen = false;
    switch (kindCopy.actionKey) {
      case 'restart':
        await updater.runRestart();
        break;
      case 'install':
        await updater.applyPendingInstall();
        break;
      case 'fetch_install':
        await updater.runUpdate();
        break;
      default:
        break;
    }
  }

  function handleDismiss() {
    popoverOpen = false;
    updater.dismiss();
  }

  // Don't render anything if no update or already dismissed for this version.
  const visible = $derived(upd.available && !upd.dismissed && upd.kind !== null);
</script>

<svelte:window onclick={handleClickOutside} />

{#if visible}
  <div class="update-wrapper" bind:this={wrapperEl}>
    <button
      class="update-trigger"
      class:updating={upd.updating}
      class:kind-binary={upd.kind === 'binary_stale'}
      class:kind-install={upd.kind === 'install_stale'}
      class:kind-remote={upd.kind === 'remote_ahead'}
      onclick={(e) => { e.stopPropagation(); popoverOpen = !popoverOpen; }}
      title={kindCopy.title}
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
          <span class="popover-title">{kindCopy.title}</span>
        </div>
        <p class="popover-desc">{kindCopy.desc}</p>
        {#if upd.error}
          <div class="popover-error">{upd.error}</div>
        {/if}
        <div class="popover-actions">
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={handleDismiss} disabled={upd.updating}>
            Later
          </button>
          <button
            class="btn-3d btn-3d-primary btn-3d-sm"
            onclick={handleAction}
            disabled={upd.updating || kindCopy.actionKey === null}
          >
            {#if upd.updating}
              Working…
            {:else}
              {kindCopy.buttonLabel}
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

  /* v0.2.16 (W4 / 0.5): visual tint per kind. Binary-stale uses an
     attention-grabbing pink (matches the green-banner pattern from
     v0.2.15's LauncherRestartBanner — same urgency); install-stale
     and remote-ahead share the default teal. */
  .update-trigger.kind-binary {
    border-color: rgba(255, 79, 160, 0.5);
    color: var(--color-pink, #ff4fa0);
  }
  .update-trigger.kind-binary:hover {
    background: rgba(255, 79, 160, 0.08);
    border-color: rgba(255, 79, 160, 0.7);
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
    width: 320px;
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
