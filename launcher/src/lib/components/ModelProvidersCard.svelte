<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!--
  v0.2.105 — Home page: "Model providers", the machine-wide per-provider
  on/off switch.

  Every presentation decision (key-state wording, the explanation of what
  switching off does, the sentence after a toggle) lives in
  `$lib/api/model_providers.ts`, which `vitest` covers without a DOM; this
  file is markup, a switch and the two calls.

  `load` / `save` are injectable so the component test drives the REAL
  component against fakes: the toggle must call the command with
  `(vendor, enabled)`, and the row must show the key STATE by name — never a
  value. Brand: glass card, teal switch, purple/muted secondary text (see
  `.claude/references/VCO_BRAND_REFERENCE.md`); the toggle styles mirror
  `RlScoringSwitch.svelte`.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import {
    anyDisabled,
    cardVisible,
    definitionsLabel,
    getModelProviders,
    keyStateLabel,
    PROVIDER_OFF_NOTICE,
    secretNamesLabel,
    setModelProviderEnabled,
    toggleNote,
    type ModelProviderRow,
    type ModelProvidersPayload,
    type ModelProviderSetResult,
  } from '$lib/api/model_providers';

  let {
    load = getModelProviders,
    save = setModelProviderEnabled,
  }: {
    load?: () => Promise<ModelProvidersPayload>;
    save?: (vendor: string, enabled: boolean) => Promise<ModelProviderSetResult>;
  } = $props();

  let payload = $state<ModelProvidersPayload | null>(null);
  let problem = $state<string | null>(null);
  let note = $state<string | null>(null);
  let busy = $state<string | null>(null);
  // Every refresh re-creates the rows. A checkbox the USER flipped already
  // holds the new DOM state, so re-rendering with the payload's (possibly
  // unchanged) `enabled` would not correct it — and after an unstored save it
  // MUST be corrected, or the card would show a change that never happened.
  let renderKey = $state(0);

  async function refresh() {
    try {
      payload = await load();
      problem = null;
    } catch (err) {
      problem = err instanceof Error ? err.message : String(err);
    }
    renderKey += 1;
  }

  onMount(() => {
    void refresh();
  });

  async function toggle(row: ModelProviderRow, enabled: boolean) {
    busy = row.vendor_id;
    note = null;
    let result: ModelProviderSetResult | null = null;
    try {
      result = await save(row.vendor_id, enabled);
    } catch {
      result = null;
    }
    await refresh();
    // A failed CALL (not a refused write) may still have stored the row:
    // the note reports what the re-read status says, never "nothing changed".
    const now = problem
      ? null
      : (payload?.providers.find((p) => p.vendor_id === row.vendor_id)?.enabled ?? null);
    note = toggleNote(row, enabled, result, now);
    busy = null;
  }
</script>

{#if problem}
  <section class="providers-card glass-card" aria-label="Model providers">
    <h2 class="providers-title">Model providers</h2>
    <p class="providers-problem" role="alert">{problem}</p>
  </section>
{:else if payload && !cardVisible(payload)}
  <!-- No gateway on this machine (or no vendors declared): the switch has
       nothing to switch, so the card renders nothing at all. -->
{:else if payload}
  <section class="providers-card glass-card" aria-label="Model providers">
    <div class="providers-head">
      <h2 class="providers-title">Model providers</h2>
      <span class="providers-tag">machine-wide</span>
    </div>
    <p class="providers-intro">
      Turn off a subscription you no longer have. Off is remembered: VCO stops
      delivering that provider's agents and hides its model rows.
    </p>

    {#key renderKey}
    <ul class="providers-list">
      {#each payload.providers as row (row.vendor_id)}
        <li class="provider-row" data-provider={row.vendor_id}>
          <div class="provider-main">
            <span class="provider-name">{row.display}</span>
            <span class="provider-detail">
              {definitionsLabel(row)} · {secretNamesLabel(row)}
              {#if row.upstream_host}<span class="provider-host">· {row.upstream_host}</span>{/if}
            </span>
          </div>
          <span
            class="provider-key"
            class:provider-key-ok={keyStateLabel(row.key_state).tone === 'ok'}
            class:provider-key-warn={keyStateLabel(row.key_state).tone === 'warn'}
            data-key-state={row.key_state}
          >{keyStateLabel(row.key_state).text}</span>
          <label class="toggle-switch">
            <input
              type="checkbox"
              checked={row.enabled}
              disabled={busy === row.vendor_id}
              aria-label="Enable {row.display}"
              data-testid="provider-switch-{row.vendor_id}"
              onchange={(e) => toggle(row, (e.target as HTMLInputElement).checked)}
            />
            <span class="toggle-slider"></span>
          </label>
        </li>
      {/each}
    </ul>
    {/key}

    {#if note}
      <p class="providers-note" data-testid="providers-note">{note}</p>
    {:else if anyDisabled(payload)}
      <p class="providers-note" data-testid="providers-off-notice">{PROVIDER_OFF_NOTICE}</p>
    {/if}
  </section>
{/if}

<style>
  .providers-card {
    margin-bottom: 22px;
    padding: 20px 22px;
    border: 1px solid rgba(255, 255, 255, 0.06);
  }

  .providers-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 6px;
  }

  .providers-title {
    font-size: 15px;
    font-weight: 800;
    color: var(--color-text);
  }

  .providers-tag {
    font-size: 11px;
    color: var(--color-teal);
    background: rgba(0, 191, 166, 0.1);
    border: 1px solid rgba(0, 191, 166, 0.25);
    border-radius: 20px;
    padding: 2px 10px;
  }

  .providers-intro {
    font-size: 12px;
    color: var(--color-mid);
    margin-bottom: 14px;
    line-height: 1.5;
  }

  .providers-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .provider-row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    align-items: center;
    gap: 14px;
    padding: 8px 0;
    border-top: 1px solid rgba(255, 255, 255, 0.04);
  }

  .provider-main {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .provider-name {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-text);
  }

  .provider-detail {
    font-size: 11px;
    color: var(--color-muted);
  }

  .provider-host {
    color: var(--color-muted);
  }

  .provider-key {
    font-size: 11px;
    color: var(--color-muted);
    white-space: nowrap;
  }

  .provider-key-ok {
    color: var(--color-teal);
  }

  .provider-key-warn {
    color: var(--color-pink);
  }

  .providers-note {
    margin-top: 12px;
    font-size: 11px;
    line-height: 1.5;
    color: var(--color-mid);
  }

  .providers-problem {
    font-size: 12px;
    color: var(--color-pink);
  }

  /* Toggle styles mirror RlScoringSwitch.svelte (Svelte scopes CSS per
     component, so the vocabulary is repeated rather than shared). */
  .toggle-switch {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
  }

  .toggle-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    inset: 0;
    background: var(--color-muted);
    border-radius: 24px;
    cursor: pointer;
    transition: background 0.3s;
  }

  .toggle-slider::before {
    content: '';
    position: absolute;
    height: 18px;
    width: 18px;
    left: 3px;
    bottom: 3px;
    background: white;
    border-radius: 50%;
    transition: transform 0.3s;
  }

  .toggle-switch input:checked + .toggle-slider {
    background: var(--color-teal);
  }

  .toggle-switch input:checked + .toggle-slider::before {
    transform: translateX(20px);
  }

  .toggle-switch input:disabled + .toggle-slider {
    opacity: 0.4;
    cursor: not-allowed;
  }
</style>