<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.102 W6-I — per project, each KG collection's RECORDED model (what
  // its searches use), the selected target and the target's fill progress.
  // W6-J: the duplicate scan moved to the project's KG tab (owner 2026-10-11);
  // this panel keeps the fill status only. Logic + the invoke live in
  // kg-embed-status.ts.
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import {
    kgEmbedFillText,
    loadKgEmbedStatus,
    type KgEmbedStatusPayload,
  } from './kg-embed-status';

  let status = $state<KgEmbedStatusPayload | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function refresh() {
    loading = true;
    error = null;
    try {
      status = await loadKgEmbedStatus({ invoke });
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

<div class="pr-kgsum-block" data-testid="kg-embed-status">
  <p class="pr-kgsum-block-label">Per-project KG collections</p>
  <p class="pr-hint">
    A collection keeps being searched with the model it is complete in. A newly selected model is
    filled in alongside (nothing is dropped) on the next whole-tree sync — VCO starts it by itself —
    and searches move to it once every object has its vector. The duplicate scan lives on the
    project's KG / Codegraph bindings tab.
  </p>
  {#if loading}
    <p class="pr-hint">Reading each project's KG model…</p>
  {:else if error}
    <p class="pr-hint pr-kgsum-warn">Couldn't read the KG models: {error}</p>
  {:else if status && status.projects.length > 0}
    {#each status.projects as p (p.folder)}
      <div class="pr-codeembed-project">
        <p class="pr-hint"><strong>{p.name}</strong> — selected <code>{p.target_model}</code></p>
        {#each p.collections as c (c.collection)}
          <p class="pr-hint">
            <code>{c.collection}</code>: searching <code>{c.recorded_model ?? '—'}</code> — {kgEmbedFillText(c)}
            {#if c.cost_bearing}· OpenAI-billed{/if}
          </p>
        {/each}
      </div>
    {/each}
  {:else}
    <p class="pr-hint">No registered project has a KG collection yet.</p>
  {/if}
</div>

<style>
  /* Same tokens as the Preferences page's code-graph status block (its
     styles are page-scoped, so this component carries the few it uses). */
  .pr-kgsum-block {
    padding: 10px 14px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    margin-top: 8px;
  }
  .pr-kgsum-block-label { font-size: 11px; font-weight: 600; color: #aaa; margin: 0 0 6px; }
  .pr-hint { font-size: 11px; color: #888; margin: 0 0 14px; line-height: 1.5; }
  .pr-hint code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .pr-kgsum-warn { color: rgb(255,184,74); margin-top: 4px; }
  .pr-codeembed-project { margin: 4px 0 8px; }
  .pr-codeembed-project code { word-break: break-all; }
</style>
