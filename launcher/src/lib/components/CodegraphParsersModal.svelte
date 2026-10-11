<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.102 W6-L — "Install code parsers?" Shown when a registered project
  // contains a language whose tree-sitter parser is missing (PowerShell and
  // Protocol Buffers are never installed by default; a missing default
  // grammar is offered the same way). Nothing is installed without the
  // user's Install:
  //
  //   checkbox → toggles that parser (all checked when the modal opens)
  //   Install  → `codegraph_parsers_install` with EXACTLY the checked set,
  //              once, into VCO's one venv (per machine, not per project);
  //              unchecked parsers are remembered as "Not now"
  //   Not now  → nothing installed; not offered again until its language
  //              appears in a project that did not contain it
  //
  // A failed install stays on screen (message + log path) until closed.
  // Decisions live in `$lib/codegraph-parsers` (pure, vitest-covered); this
  // is the shell. Styling: DialogRoot + the app.css 3D buttons + brand
  // tokens (.claude/references/VCO_BRAND_REFERENCE.md), the same recipe as
  // GatewayRestartModal.

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { codegraphParsers } from '$lib/stores/codegraph-parsers';
  import {
    explanation,
    joinNames,
    languageName,
    MODAL_TITLE,
    projectName,
  } from '$lib/codegraph-parsers';

  const state = $derived($codegraphParsers);
  const intro = $derived(explanation(state.offers));
  const settled = $derived(state.result !== null || state.error !== null);

  // Escape / dialog close: after a result it is Close, before it Not now.
  // One unconditional handler (DialogRoot's onClose is a notification).
  function dismiss(): void {
    if (settled) codegraphParsers.close();
    else void codegraphParsers.notNow();
  }
</script>

{#if state.open}
  <DialogRoot
    open={true}
    width="520px"
    ariaLabelledBy="cpm-title"
    closeOnBackdrop={false}
    closeOnEscape={!state.installing}
    onClose={dismiss}
  >
    {#snippet header()}
      <div class="cpm-header">
        <img class="cpm-logo" src="/logo-512.png" alt="" aria-hidden="true" />
        <div>
          <h3 id="cpm-title">{MODAL_TITLE}</h3>
          <span class="cpm-tag">Code graph · parsers</span>
        </div>
      </div>
    {/snippet}
    {#snippet body()}
      <p class="cpm-message">{intro}</p>
      <ul class="cpm-list">
        {#each state.offers as offer (offer.id)}
          <li>
            <label class="cpm-row" class:cpm-row-off={!state.selected.includes(offer.id)}>
              <input
                type="checkbox"
                data-testid="cpm-parser-{offer.id}"
                checked={state.selected.includes(offer.id)}
                disabled={state.installing || settled}
                onchange={() => codegraphParsers.toggle(offer.id)}
              />
              <span class="cpm-label">{offer.label} parser</span>
              <span class="cpm-detail">
                {joinNames(offer.languages.map(languageName))} in
                {joinNames(offer.projects.map(projectName))}
              </span>
            </label>
          </li>
        {/each}
      </ul>

      {#if state.installing}
        <div class="cpm-progress" role="status">
          <span class="cpm-spinner" aria-hidden="true"></span>
          Installing into VCO's environment and checking each parser loads…
        </div>
      {/if}

      {#if state.result}
        <div class="cpm-result" class:cpm-result-ok={state.result.ok} role="status">
          {state.result.message}
          {#if state.result.ok}
            The next code-graph update reads those files with the new parsers.
          {/if}
          {#each state.result.failed as f (f.id)}
            <div class="cpm-mono">{f.id}: {f.error}</div>
          {/each}
          {#if !state.result.ok && state.result.log_path}
            <div class="cpm-mono">Log: {state.result.log_path}</div>
          {/if}
        </div>
      {/if}

      {#if state.error}
        <div class="cpm-error" role="alert">Install failed: {state.error}</div>
      {/if}
    {/snippet}
    {#snippet footer()}
      <div class="cpm-footer">
        {#if settled}
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={() => codegraphParsers.close()}>
            Close
          </button>
        {:else}
          <button
            class="btn-3d btn-3d-ghost btn-3d-sm"
            disabled={state.installing}
            onclick={() => codegraphParsers.notNow()}
          >
            Not now
          </button>
          <button
            class="btn-3d btn-3d-primary btn-3d-sm"
            data-testid="cpm-install"
            disabled={state.installing || state.selected.length === 0}
            onclick={() => codegraphParsers.install()}
          >
            {state.installing ? 'Installing…' : 'Install'}
          </button>
        {/if}
      </div>
    {/snippet}
  </DialogRoot>
{/if}

<style>
  .cpm-header {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .cpm-logo {
    width: 36px;
    height: 36px;
  }
  .cpm-header h3 {
    margin: 0;
    font-size: 16px;
    font-weight: 700;
    color: var(--color-text);
  }
  .cpm-tag {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--color-teal);
  }
  .cpm-message {
    margin: 0 0 12px;
    font-size: 13px;
    line-height: 1.6;
    color: var(--color-text);
  }
  .cpm-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .cpm-row {
    display: grid;
    grid-template-columns: auto 1fr;
    column-gap: 10px;
    align-items: center;
    padding: 10px 12px;
    background: var(--color-card);
    border: 1px solid rgba(0, 191, 166, 0.3);
    border-radius: 12px;
    cursor: pointer;
    transition: border-color 0.2s ease, opacity 0.2s ease;
  }
  .cpm-row-off {
    border-color: var(--color-border);
    opacity: 0.7;
  }
  .cpm-row input {
    grid-row: span 2;
    accent-color: var(--color-teal);
    width: 16px;
    height: 16px;
  }
  .cpm-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-text);
  }
  .cpm-detail {
    font-size: 12px;
    color: var(--color-mid);
  }
  .cpm-progress {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 12px;
    font-size: 12px;
    color: var(--color-mid);
  }
  .cpm-spinner {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2px solid rgba(0, 191, 166, 0.25);
    border-top-color: var(--color-teal);
    animation: cpm-spin 0.9s linear infinite;
  }
  @keyframes cpm-spin {
    to {
      transform: rotate(360deg);
    }
  }
  .cpm-result {
    margin-top: 12px;
    padding: 10px 12px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--color-text);
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 10px;
  }
  .cpm-result-ok {
    background: rgba(0, 191, 166, 0.08);
    border-color: rgba(0, 191, 166, 0.35);
  }
  .cpm-mono {
    margin-top: 4px;
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    font-size: 11px;
    word-break: break-word;
    color: var(--color-mid);
  }
  .cpm-error {
    margin-top: 12px;
    padding: 10px 12px;
    font-size: 12px;
    color: var(--color-pink);
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 10px;
  }
  .cpm-footer {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }
</style>
