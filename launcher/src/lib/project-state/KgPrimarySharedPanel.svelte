<!--
  SPDX-License-Identifier: AGPL-3.0-or-later

  v0.2.105: Identity-tab banner shown when this (non-root) project's PRIMARY
  KG binding names the SHARED KG collection — the state the ledger reports as
  `kg_primary_bound_to_shared`. Detection: `get_kg_primary_shared_status`
  (Python SSOT vco_lib.kg_primary_shared). Repair: `update_project_identity`
  with the name-derived class (re-points the binding + re-projects the env;
  nothing is moved or deleted). Shows nothing when the check cannot run.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { UpdateProjectIdentityResult } from '$lib/types/identity';
  import { bannerMessage, repairRequest, type KgPrimarySharedStatus } from './kg-primary-shared';

  let {
    projectId,
    onRepaired,
  }: { projectId: string; onRepaired?: (res: UpdateProjectIdentityResult) => void } = $props();

  let status = $state<KgPrimarySharedStatus | null>(null);
  let repairing = $state(false);

  const message = $derived(bannerMessage(status));
  const request = $derived(repairRequest(status));

  async function check() {
    try {
      status = await invoke<KgPrimarySharedStatus>('get_kg_primary_shared_status', { projectId });
    } catch {
      status = null; // could not look: no verdict, no banner
    }
  }

  async function repair() {
    const req = repairRequest(status);
    if (!req) return;
    repairing = true;
    try {
      const res = await invoke<UpdateProjectIdentityResult>('update_project_identity', {
        projectId,
        req,
      });
      for (const w of res.warnings) toast.error(`Identity warning: ${w}`);
      toast.success(`Primary KG binding repaired: ${res.identity.kg_collection}`);
      onRepaired?.(res);
      await check();
    } catch (e) {
      toast.error(e);
    } finally {
      repairing = false;
    }
  }

  onMount(check);
</script>

{#if message}
  <div class="ps-section kg-primary-shared" role="alert">
    <h4>Primary KG is the shared collection</h4>
    <p class="ps-form-hint">{message}</p>
    {#if request}
      <button class="ps-btn-primary" type="button" onclick={repair} disabled={repairing}>
        {repairing ? 'Repairing…' : 'Repair primary KG binding'}
      </button>
    {/if}
  </div>
{/if}

<style>
  .kg-primary-shared {
    border: 1px solid rgba(var(--color-pink-rgb), 0.45);
    background: rgba(var(--color-pink-rgb), 0.08);
    border-radius: 12px;
  }
  .kg-primary-shared h4 {
    color: var(--color-pink);
  }
</style>
