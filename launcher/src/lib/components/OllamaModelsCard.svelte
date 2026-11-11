<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.106 (owner ruling 2026-11-11) — Services → Ollama models: what the
  // hub sees loaded in Ollama, how much RAM / VRAM each takes, when the hub
  // will unload it (or who else holds it), and "Unload now". Everything goes
  // through the hub (`/api/v1/ollama/models`), which owns the unloading; the
  // sentences come from `$lib/api/ollama_lifetime` (vitest-pinned).
  import { onDestroy, onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { pollWhileActive } from '$lib/window-activity';
  import {
    holderLine,
    humanDuration,
    loadOllamaModels,
    memoryLine,
    unloadOllamaModel,
    type OllamaModelsStatus,
  } from '$lib/api/ollama_lifetime';

  let status = $state<OllamaModelsStatus | null>(null);
  let error = $state<string | null>(null);
  let busy = $state<string | null>(null);
  let actionError = $state<string | null>(null);
  let now = $state(new Date());

  async function refresh() {
    try {
      status = await loadOllamaModels({ invoke });
      error = null;
    } catch (e) {
      status = null;
      error = String(e);
    }
    now = new Date();
  }

  async function unload(model: string) {
    busy = model;
    actionError = null;
    try {
      await unloadOllamaModel({ invoke }, model);
    } catch (e) {
      actionError = `Couldn't unload ${model}: ${e}`;
    } finally {
      busy = null;
      await refresh();
    }
  }

  let stopPolling: (() => void) | null = null;
  onMount(() => {
    void refresh();
    stopPolling = pollWhileActive(() => void refresh(), 15_000);
  });
  onDestroy(() => stopPolling?.());
</script>

<section class="ollama-card" data-testid="ollama-models-card">
  <header class="om-head">
    <h2>Ollama models</h2>
    {#if status}
      <span class="muted small">
        idle unload: {humanDuration(status.idle)} · checked every {status.poll_secs} s by the hub
      </span>
    {/if}
  </header>
  <p class="muted">
    Models Ollama has in memory right now. The background hub unloads one when no VCO request has
    used it for the time set in Preferences → Ollama model unloading.
  </p>
  {#if error}
    <p class="om-warn">The hub is not answering, so the list is unavailable: {error}</p>
  {:else if status && !status.reachable}
    <p class="om-warn">Ollama is not reachable: {status.error ?? 'no answer'}</p>
  {:else if status && status.models.length === 0}
    <p class="muted small">No model is loaded.</p>
  {:else if status}
    <table class="om-table">
      <thead>
        <tr><th>Model</th><th>Memory</th><th>Unloading</th><th></th></tr>
      </thead>
      <tbody>
        {#each status.models as m (m.name)}
          <tr>
            <td><code>{m.name}</code></td>
            <td>{memoryLine(m)}</td>
            <td class="small">{holderLine(m, now)}</td>
            <td>
              <button
                class="om-btn"
                disabled={busy !== null}
                onclick={() => void unload(m.name)}
              >{busy === m.name ? 'Unloading…' : 'Unload now'}</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="muted small">Reading…</p>
  {/if}
  {#if actionError}
    <p class="om-warn">{actionError}</p>
  {/if}
  {#if status && status.recent_unloads.length > 0}
    <h3>Recent unloads</h3>
    <ul class="om-events small">
      {#each status.recent_unloads.slice(0, 5) as ev, i (i)}
        <li>
          {ev.at ? new Date(ev.at).toLocaleTimeString() : '—'} · <code>{ev.model}</code> ·
          {ev.reason === 'manual' ? 'Unload now' : 'idle'} ·
          {ev.ok ? 'unloaded' : `failed: ${ev.error ?? 'unknown error'}`}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  /* Same tokens as the Services page's gateway card (page-scoped there). */
  .ollama-card {
    margin-top: 2rem;
    padding: 1rem 1.1rem 1.2rem;
    border: 1px solid var(--border, #333);
    border-radius: 6px;
  }
  .om-head { display: flex; align-items: baseline; gap: 0.8rem; flex-wrap: wrap; }
  .om-head h2 { margin: 0; }
  .ollama-card h3 { margin: 1.2rem 0 0.3rem; font-size: 0.95rem; }
  .muted { opacity: 0.75; }
  .small { font-size: 0.85rem; }
  .om-warn { color: #f5b942; }
  .om-table { width: 100%; border-collapse: collapse; margin-top: 0.6rem; }
  .om-table th, .om-table td { text-align: left; padding: 0.35rem 0.5rem; vertical-align: top; }
  .om-table tbody tr { border-top: 1px solid var(--border, #333); }
  .om-btn {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 12px; border-radius: 4px; cursor: pointer; white-space: nowrap;
  }
  .om-btn:disabled { opacity: 0.5; cursor: default; }
  .om-events { margin: 0; padding-left: 1.1rem; }
</style>
