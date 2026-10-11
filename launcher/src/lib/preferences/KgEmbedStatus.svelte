<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.102 W6-I — per project, each KG collection's RECORDED model (what
  // its searches use), the selected target and the target's fill progress;
  // plus the duplicate scan, whose "objects missing the vector" list is shown
  // with its explanation. Pure logic + the invokes live in kg-embed-status.ts.
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import {
    hasSlotsText,
    kgEmbedFillText,
    loadKgEmbedStatus,
    missingVectorExplanation,
    runDuplicateScan,
    type DuplicateScanResult,
    type KgEmbedStatusPayload,
  } from './kg-embed-status';

  let status = $state<KgEmbedStatusPayload | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let scans = $state<Record<string, DuplicateScanResult>>({});
  let scanning = $state<Record<string, boolean>>({});
  let scanErrors = $state<Record<string, string>>({});

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

  async function scan(projectId: string) {
    scanning = { ...scanning, [projectId]: true };
    scanErrors = { ...scanErrors, [projectId]: '' };
    try {
      const result = await runDuplicateScan(projectId, { invoke });
      scans = { ...scans, [projectId]: result };
    } catch (e) {
      scanErrors = { ...scanErrors, [projectId]: String(e) };
    } finally {
      scanning = { ...scanning, [projectId]: false };
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
    and searches move to it once every object has its vector.
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
        {#if p.project_id}
          {@const pid = p.project_id}
          <button
            type="button"
            class="pr-btn"
            disabled={scanning[pid]}
            onclick={() => void scan(pid)}
          >{scanning[pid] ? 'Scanning…' : 'Check for duplicates'}</button>
          {#if scanErrors[pid]}
            <p class="pr-hint pr-kgsum-warn">Duplicate scan failed: {scanErrors[pid]}</p>
          {/if}
          {#if scans[pid]}
            {@const r = scans[pid]}
            <p class="pr-hint">
              {r.pairs.length} likely duplicate pair(s) at threshold {r.threshold}{#if r.nodes_skipped > 0}
                · {r.nodes_skipped} node(s) re-synced during the scan were not compared{/if}
            </p>
            {#if r.objects_missing_vector.length > 0}
              <p class="pr-hint pr-kgsum-warn" data-testid="kg-missing-vector-why">
                {missingVectorExplanation(r.objects_missing_vector)}
              </p>
              <ul class="pr-hint" data-testid="kg-missing-vector-list">
                {#each r.objects_missing_vector as o (o.uuid)}
                  <li><strong>{o.title}</strong> <code>{o.path}</code> ({hasSlotsText(o)})</li>
                {/each}
              </ul>
            {/if}
          {/if}
        {/if}
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
  .pr-btn {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 5px 14px; border-radius: 4px; cursor: pointer;
    font-size: 12px; font-weight: 500; white-space: nowrap;
  }
  .pr-btn:hover { background: rgba(255,255,255,0.09); }
  .pr-btn:disabled { opacity: 0.55; cursor: not-allowed; }
</style>
