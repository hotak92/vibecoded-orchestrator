<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // v0.2.91 WP-I (decision #6) — MenuBar badge for the ORCHESTRATOR-ROOT
  // deferral ledger.
  //
  // SCOPE, stated because the MenuBar is the one place it could be ambiguous:
  // the MenuBar is global chrome, so this badge counts the ORCHESTRATOR ROOT's
  // ledger and nothing else. It never sums in the selected project's entries —
  // those have their own badge on their own Settings panel. Every string here
  // says "orchestrator root" so a user can never read it as "3 things wrong
  // with the project I have selected".
  //
  // WHAT it counts: `action_required` only (user decision 2026-08-27, via
  // `badgeCount`). Conditions VCO retries by itself are listed in the panel but
  // never badged here — chrome must not nag about work the reader cannot act
  // on. The popover says so, so the number and the list can never look like a
  // disagreement.
  //
  // Modelled on UpdateBadge's Continue-Update precedent: an icon + dot that
  // renders ONLY when there is something to say, a popover with the honest
  // sentence, and a button that navigates to the surface that owns the detail
  // (Preferences → Updates) rather than duplicating the list in a popover.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { deferrals } from '$lib/stores/deferrals';
  import { badgeCount, ROOT_SCOPE_LABEL } from '$lib/deferral-ledger';

  let popoverOpen = $state(false);
  let wrapperEl = $state<HTMLDivElement | null>(null);

  const rootLedger = $derived($deferrals);
  const count = $derived(badgeCount(rootLedger.view));
  // Renders only for REAL pending work. A ledger holding nothing but records
  // is not a nag — that tiering is the entire point of WP-B.
  const visible = $derived(rootLedger.loaded && count > 0);

  onMount(() => {
    void deferrals.refreshRoot();
  });

  function handleClickOutside(e: MouseEvent) {
    if (popoverOpen && wrapperEl && !wrapperEl.contains(e.target as Node)) {
      popoverOpen = false;
    }
  }

  function openLedger() {
    popoverOpen = false;
    void goto('/preferences/updates');
  }
</script>

<svelte:window onclick={handleClickOutside} />

{#if visible}
  <div class="dfb-wrapper" bind:this={wrapperEl}>
    <button
      class="dfb-trigger"
      onclick={(e) => {
        e.stopPropagation();
        popoverOpen = !popoverOpen;
      }}
      title={`${ROOT_SCOPE_LABEL}: ${count} pending action${count === 1 ? '' : 's'}`}
      aria-label={`${ROOT_SCOPE_LABEL}: ${count} pending action${count === 1 ? '' : 's'}`}
    >
      <!-- clipboard-list: reads as "a list of things to do", distinct from
           UpdateBadge's refresh arrows and its alert triangle. -->
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
        <rect x="8" y="2" width="8" height="4" rx="1" />
        <line x1="9" y1="12" x2="15" y2="12" />
        <line x1="9" y1="16" x2="13" y2="16" />
      </svg>
      <span class="dfb-count">{count}</span>
    </button>

    {#if popoverOpen}
      <div class="dfb-popover">
        <div class="dfb-header">
          <span class="dfb-title">{ROOT_SCOPE_LABEL} — pending actions</span>
        </div>
        <p class="dfb-desc">
          {count} deferred condition{count === 1 ? '' : 's'} on the orchestrator
          install {count === 1 ? 'needs' : 'need'} you to act. Two things this
          count is not: it is not the selected project's — a project's own
          deferrals appear on that project's Settings tab; and it is not
          everything open — conditions VCO retries by itself are listed in the
          panel but never badged.
        </p>
        {#if rootLedger.view?.folder}
          <p class="dfb-folder"><code>{rootLedger.view.folder}</code></p>
        {/if}
        <div class="dfb-actions">
          <button
            class="btn-3d btn-3d-ghost btn-3d-sm"
            onclick={() => (popoverOpen = false)}
          >
            Later
          </button>
          <button class="btn-3d btn-3d-primary btn-3d-sm" onclick={openLedger}>
            Review
          </button>
        </div>
      </div>
    {/if}
  </div>
{/if}

<style>
  .dfb-wrapper {
    position: relative;
  }
  .dfb-trigger {
    position: relative;
    display: flex;
    align-items: center;
    gap: 5px;
    height: 32px;
    padding: 0 10px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(245, 179, 66, 0.4);
    border-radius: 10px;
    color: #f5b342;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .dfb-trigger:hover {
    background: rgba(245, 179, 66, 0.08);
    border-color: rgba(245, 179, 66, 0.65);
  }
  .dfb-count {
    font-size: 11px;
    font-weight: 700;
    line-height: 1;
  }
  .dfb-popover {
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
  }
  .dfb-header {
    margin-bottom: 6px;
  }
  .dfb-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-text);
  }
  .dfb-desc {
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
    margin-bottom: 8px;
  }
  .dfb-folder {
    margin-bottom: 10px;
  }
  .dfb-folder code {
    font-family: ui-monospace, monospace;
    font-size: 10px;
    color: #c4b3ff;
    word-break: break-all;
  }
  .dfb-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
