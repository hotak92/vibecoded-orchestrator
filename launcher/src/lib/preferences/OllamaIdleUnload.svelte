<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.106 (owner ruling 2026-11-11) — Preferences → Ollama model
  // unloading. ONE machine-wide idle time; the hub reads it and unloads the
  // model. Every decision lives in `$lib/api/ollama_lifetime` (vitest-pinned);
  // validation is the Rust parser the hub resolves with.
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import Dropdown from '$lib/components/Dropdown.svelte';
  import {
    CUSTOM_PRESET,
    IDLE_PRESETS,
    envOverrideNote,
    idleSummary,
    loadOllamaIdle,
    presetFor,
    saveOllamaIdle,
    type OllamaIdleState,
  } from '$lib/api/ollama_lifetime';

  let idle = $state<OllamaIdleState | null>(null);
  let preset = $state<string>('10m');
  let custom = $state('');
  let loading = $state(true);
  let saving = $state(false);
  let saved = $state(false);
  let error = $state<string | null>(null);

  function apply(next: OllamaIdleState) {
    idle = next;
    preset = presetFor(next);
    custom = preset === CUSTOM_PRESET ? (next.stored ?? '') : '';
  }

  async function save(value: string | null) {
    saving = true;
    saved = false;
    error = null;
    try {
      apply(await saveOllamaIdle({ invoke }, value));
      saved = true;
    } catch (e) {
      error = String(e);
    } finally {
      saving = false;
    }
  }

  function onPreset(v: string) {
    preset = v;
    if (v !== CUSTOM_PRESET) void save(v);
  }

  onMount(async () => {
    try {
      apply(await loadOllamaIdle({ invoke }));
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  });
</script>

<div data-testid="ollama-idle-unload">
  <div class="oiu-row">
    <div class="oiu-text">
      <strong>Unload an idle model after</strong>
      <span class="oiu-hint">
        How long Ollama keeps an embedding or summary model in memory after the last VCO request
        that used it. Shorter frees RAM / VRAM sooner (a model can take 1–3 GB); the next lookup
        then waits about 2 seconds while the model loads again. While you work, the knowledge-graph
        lookups made on every tool call keep using the model, so it stays loaded unless the gap
        between them is longer than this.
        <br /><br />
        The background hub applies it to every project: it checks the loaded models every few
        seconds and picks up a change on its next check, with nothing to restart. If the hub is not
        running, a model still unloads on its own after {idle?.caller_keep_alive ?? '45m'}.
        To keep a model loaded for good, set <code>VCO_OLLAMA_KEEP_ALIVE</code> (for example
        <code>-1</code>) — it wins over this setting, and the hub leaves those models alone.
      </span>
    </div>
    <div class="oiu-dd">
      <Dropdown
        options={IDLE_PRESETS}
        value={preset}
        disabled={loading || saving}
        ariaLabel="Unload an idle Ollama model after"
        onChange={(v: string) => onPreset(v)}
      />
    </div>
  </div>
  {#if preset === CUSTOM_PRESET}
    <div class="oiu-custom">
      <input
        type="text"
        class="oiu-input"
        placeholder="e.g. 15m, 90s, 1m30s"
        aria-label="Idle time before unloading"
        bind:value={custom}
        disabled={saving}
        onkeydown={(e) => {
          if (e.key === 'Enter') void save(custom);
        }}
      />
      <button class="oiu-btn" disabled={saving || custom.trim() === ''} onclick={() => void save(custom)}>
        Save
      </button>
      <span class="oiu-hint">Whole hours, minutes or seconds, up to {idle?.max_idle ?? '30m'}.</span>
    </div>
  {/if}
  {#if idle}
    <p class="oiu-hint oiu-summary">
      {idleSummary(idle)}
      {#if idle.stored !== null}
        <button class="oiu-link" disabled={saving} onclick={() => void save(null)}>Use the default</button>
      {/if}
    </p>
    {#if envOverrideNote(idle)}
      <p class="oiu-warn">{envOverrideNote(idle)}</p>
    {/if}
  {/if}
  {#if saving}
    <p class="oiu-hint">Saving…</p>
  {:else if saved}
    <p class="oiu-hint oiu-saved">Saved!</p>
  {/if}
  {#if error}
    <p class="oiu-error">Couldn't save: {error}</p>
  {/if}
</div>

<style>
  /* The Preferences page's row tokens (its styles are page-scoped, so this
     component carries the few it uses — same as KgEmbedStatus). */
  .oiu-row {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 10px 14px; background: rgba(255,255,255,0.03); border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .oiu-text { display: flex; flex-direction: column; gap: 3px; }
  .oiu-text strong { font-size: 12px; color: #ccc; font-weight: 500; }
  .oiu-hint { font-size: 11px; color: #888; max-width: 480px; line-height: 1.5; }
  .oiu-hint code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .oiu-dd { width: 260px; }
  .oiu-custom { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
  .oiu-input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 5px 8px; border-radius: 4px; font-size: 12px; width: 140px;
  }
  .oiu-btn {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 5px 14px; border-radius: 4px; cursor: pointer;
    font-size: 12px; font-weight: 500;
  }
  .oiu-btn:hover { background: rgba(255,255,255,0.09); }
  .oiu-summary { margin: 8px 0 0; max-width: none; }
  .oiu-link {
    background: none; border: none; color: rgb(0,191,166); cursor: pointer;
    font-size: 11px; padding: 0 0 0 6px; text-decoration: underline;
  }
  .oiu-warn { font-size: 11px; color: rgb(255,184,74); margin: 6px 0 0; line-height: 1.5; }
  .oiu-saved { color: rgb(0,191,166); font-weight: 500; }
  .oiu-error {
    margin-top: 8px; padding: 8px 12px;
    background: rgba(229,77,77,0.1); border: 1px solid rgba(229,77,77,0.25);
    border-radius: 4px; color: rgb(255,140,140); font-size: 11px;
  }
</style>
