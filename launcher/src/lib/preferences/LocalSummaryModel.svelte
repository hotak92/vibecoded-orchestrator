<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.106 (owner ruling 2026-11-12) — Preferences → KG Summaries →
  // "Local summary model". Every decision lives in
  // `$lib/api/local_summary_model` (vitest-pinned); this is markup over it.
  import { onMount } from 'svelte';
  import { invoke, listen } from '$lib/tauri';
  import { MODEL_ENSURE_EVENT, type ModelEnsureEvent } from '$lib/dual-flags';
  import {
    canDownload,
    canRemove,
    downloadLocalSummaryModel,
    downloadOutcomeText,
    envOverrideNote,
    loadLocalSummaryStatus,
    presenceLine,
    removeLabel,
    removeLocalSummaryModel,
    removeUnavailableNote,
    saveLocalSummaryEnabled,
    toggleDescription,
    type LocalSummaryStatus,
  } from '$lib/api/local_summary_model';

  let st = $state<LocalSummaryStatus | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let note = $state<string | null>(null);
  let error = $state<string | null>(null);
  let downloading = $state(false);

  async function refresh() {
    try {
      st = await loadLocalSummaryStatus({ invoke });
      error = null;
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function toggle(next: boolean) {
    busy = true;
    note = null;
    error = null;
    try {
      await saveLocalSummaryEnabled({ invoke }, next);
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }

  async function remove() {
    if (!st) return;
    busy = true;
    note = null;
    error = null;
    try {
      const res = await removeLocalSummaryModel({ invoke, confirm: (m: string) => confirm(m) }, st);
      if (res !== null) {
        note = `${st.model} removed.`;
        await refresh();
      }
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }

  async function download() {
    busy = true;
    note = null;
    error = null;
    try {
      downloading = await downloadLocalSummaryModel({ invoke });
      note = downloading
        ? 'Download started in the background — it can take a few minutes.'
        : 'Could not start the download here; the next install/update downloads it.';
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void refresh();
    // The host-wide ensure "Download now" started reports here when done.
    const un = listen<ModelEnsureEvent>(MODEL_ENSURE_EVENT, (e) => {
      if (!downloading || e.payload.project_id !== null || !st) return;
      downloading = false;
      const r = downloadOutcomeText(e.payload.outcome, st.model);
      if (r.ok) note = r.text;
      else error = r.text;
      void refresh();
    });
    return () => void un.then((f) => f());
  });
</script>

<div data-testid="local-summary-model">
  <div class="lsm-row">
    <div class="lsm-text">
      <strong>Local summary model</strong>
      <span class="lsm-hint">
        {#if st}{toggleDescription(st)}{:else}Downloads a local Ollama model as a fallback when
          Claude can't summarise. Turn off to save disk; summaries then use Claude/cloud only.{/if}
      </span>
    </div>
    <label class="lsm-switch">
      <input
        type="checkbox"
        data-testid="local-summary-model-toggle"
        checked={st?.enabled ?? true}
        disabled={loading || busy || !st}
        onchange={(e) => void toggle((e.currentTarget as HTMLInputElement).checked)}
      />
      <span>{st?.enabled ?? true ? 'On' : 'Off'}</span>
    </label>
  </div>
  {#if st}
    <p class="lsm-hint lsm-line">
      {presenceLine(st)}
      {#if canRemove(st)}
        <button class="lsm-btn" data-testid="local-summary-model-remove" disabled={busy} onclick={() => void remove()}>
          {removeLabel(st)}
        </button>
      {/if}
      {#if canDownload(st)}
        <button class="lsm-btn" data-testid="local-summary-model-download" disabled={busy} onclick={() => void download()}>
          Download now
        </button>
      {/if}
    </p>
    {#if removeUnavailableNote(st)}
      <p class="lsm-hint lsm-line">{removeUnavailableNote(st)}</p>
    {/if}
    {#if envOverrideNote(st)}
      <p class="lsm-warn">{envOverrideNote(st)}</p>
    {/if}
  {/if}
  {#if note}
    <p class="lsm-hint lsm-ok">{note}</p>
  {/if}
  {#if error}
    <p class="lsm-error">{error}</p>
  {/if}
</div>

<style>
  /* The Preferences page's row tokens (its styles are page-scoped, so this
     component carries the few it uses — same as OllamaIdleUnload). */
  .lsm-row {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 10px 14px; background: rgba(255,255,255,0.03); border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .lsm-text { display: flex; flex-direction: column; gap: 3px; }
  .lsm-text strong { font-size: 12px; color: #ccc; font-weight: 500; }
  .lsm-hint { font-size: 11px; color: #888; max-width: 480px; line-height: 1.5; }
  .lsm-line { margin: 8px 0 0; max-width: none; }
  .lsm-switch { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #ccc; }
  .lsm-btn {
    margin-left: 8px;
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 12px; border-radius: 4px; cursor: pointer;
    font-size: 11px; font-weight: 500;
  }
  .lsm-btn:hover { background: rgba(255,255,255,0.09); }
  .lsm-warn { font-size: 11px; color: rgb(255,184,74); margin: 6px 0 0; line-height: 1.5; }
  .lsm-ok { color: rgb(0,191,166); margin: 6px 0 0; }
  .lsm-error {
    margin-top: 8px; padding: 8px 12px;
    background: rgba(229,77,77,0.1); border: 1px solid rgba(229,77,77,0.25);
    border-radius: 4px; color: rgb(255,140,140); font-size: 11px;
  }
</style>
