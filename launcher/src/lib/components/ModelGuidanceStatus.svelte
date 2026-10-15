<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.103 (plan item 12) — which model providers were detected on this
  // machine and when the per-project model guidance was last refreshed.
  // Display-only (the refresh itself is automatic: launcher startup, every
  // key change in this panel, every install/update). Logic + the invoke
  // live in model-guidance-status.ts.
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import {
    lastRefreshText,
    loadModelGuidanceStatus,
    vendorRows,
    vendorStateText,
    type ModelGuidanceStatusPayload,
  } from './model-guidance-status';

  let status = $state<ModelGuidanceStatusPayload | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function refresh() {
    loading = true;
    error = null;
    try {
      status = await loadModelGuidanceStatus({ invoke });
    } catch (e) {
      status = null;
      error = String(e);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void refresh();
  });
</script>

<div class="mg-block" data-testid="model-guidance-status">
  <p class="mg-block-label">Model providers &amp; guidance</p>
  <p class="mg-hint">
    The model-selection guidance in each project's <code>CLAUDE.md</code> lists only the
    models you can actually call from this machine. It re-renders by itself at launcher
    startup, on every key change in this panel, and at every install/update.
  </p>
  {#if loading}
    <p class="mg-hint">Checking which provider keys resolve…</p>
  {:else if error}
    <p class="mg-hint mg-warn">Couldn't read the provider status: {error}</p>
  {:else if status}
    {#each vendorRows(status) as v (v.id)}
      {@const state = vendorStateText(v)}
      <p class="mg-row">
        <span class="mg-chip mg-chip-{state.tone}">{state.label}</span>
        <strong>{v.display}</strong>
        <span class="mg-row-text">{state.text}</span>
      </p>
    {/each}
    <p class="mg-refresh">{lastRefreshText(status.last_refresh)}</p>
  {/if}
</div>

<style>
  /* Same tokens as the secrets page's own blocks (its styles are
     page-scoped, so this component carries the few it uses). */
  .mg-block {
    padding: 10px 14px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    margin-bottom: 14px;
  }
  .mg-block-label {
    font-size: 11px;
    font-weight: 600;
    color: #aaa;
    margin: 0 0 6px;
  }
  .mg-hint {
    font-size: 11px;
    color: #888;
    margin: 0 0 10px;
    line-height: 1.5;
  }
  .mg-hint code {
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .mg-warn {
    color: rgb(255, 184, 74);
  }
  .mg-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 11px;
    color: #aaa;
    margin: 0 0 6px;
    line-height: 1.5;
  }
  .mg-row-text {
    color: #888;
  }
  .mg-chip {
    flex: none;
    font-size: 10px;
    font-weight: 600;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px solid transparent;
  }
  .mg-chip-ok {
    color: var(--color-teal, rgb(0, 191, 166));
    border-color: rgba(0, 191, 166, 0.45);
    background: rgba(0, 191, 166, 0.1);
  }
  .mg-chip-off {
    color: #888;
    border-color: rgba(255, 255, 255, 0.14);
    background: rgba(255, 255, 255, 0.04);
  }
  .mg-chip-warn {
    color: rgb(255, 184, 74);
    border-color: rgba(255, 184, 74, 0.4);
    background: rgba(255, 184, 74, 0.08);
  }
  .mg-refresh {
    font-size: 11px;
    color: #888;
    margin: 8px 0 0;
    line-height: 1.5;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
    padding-top: 8px;
  }
</style>
