<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.107 (A-003) — Preferences → Services → "Owed background work".
  // Every decision lives in `$lib/api/owed_work` (vitest-pinned); this is
  // markup over it. Read-only: the queue drains by itself.
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { loadOwedWorkStatus, queueLine, type OwedWorkStatus } from '$lib/api/owed_work';

  let st = $state<OwedWorkStatus | null>(null);
  let error = $state<string | null>(null);

  async function refresh() {
    try {
      st = await loadOwedWorkStatus({ invoke });
      error = null;
    } catch (e) {
      error = String(e);
    }
  }

  onMount(refresh);
</script>

<div class="pr-onboarding-row" data-testid="owed-work-queue">
  <div class="pr-onboarding-text">
    <strong>Owed background work</strong>
    {#if error}
      <span class="pr-onboarding-hint pr-startup-error">{error}</span>
    {:else if st}
      <span class="pr-onboarding-hint">{queueLine(st)}</span>
    {:else}
      <span class="pr-onboarding-hint">Reading the queue…</span>
    {/if}
  </div>
  <button type="button" class="owq-btn" data-testid="owed-work-refresh" onclick={() => void refresh()}>Refresh</button>
</div>

<style>
  .owq-btn {
    margin-left: 8px;
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 12px; border-radius: 4px; cursor: pointer;
    font-size: 11px; font-weight: 500;
  }
  .owq-btn:hover { background: rgba(255,255,255,0.09); }
</style>
