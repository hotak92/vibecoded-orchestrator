<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.97 — "The model gateway needs restarting" after an orchestrator
  // update. Shown ONLY when the backend has proven the running gateway is on
  // older code than the checkout (`model_gateway_freshness` → `prompt`).
  //
  //   Continue → `model_gateway_restart_stale` (the ONLY restart path; the
  //              backend re-checks and refuses unless still proven stale).
  //   Dismiss  → nothing is restarted; this exact situation is remembered so
  //              the next launcher start does not ask again (a new update or
  //              a restarted gateway asks afresh).
  //
  // Never automatic: the VS Code panel routes every chat through the gateway,
  // so a restart ends any live agent session — hence the "make sure no agent
  // is running" wording, and a Continue the user must press. Decisions live
  // in `$lib/gateway-freshness` (pure, vitest-covered); this is the shell.
  // Styling: DialogRoot + the app.css 3D buttons + brand tokens
  // (.claude/references/VCO_BRAND_REFERENCE.md).

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { gatewayFreshness } from '$lib/stores/gateway-freshness';
  import { canContinue, MODAL_MESSAGE, MODAL_TITLE } from '$lib/gateway-freshness';

  const state = $derived($gatewayFreshness);
  const report = $derived(state.report);
  const restartable = $derived(canContinue(report));
  const versionLine = $derived(
    report?.running_version && report?.checkout_version &&
      report.running_version !== report.checkout_version
      ? `Running ${report.running_version}; updated to ${report.checkout_version}.`
      : null,
  );
</script>

{#if state.open && report}
  <DialogRoot
    open={true}
    width="520px"
    ariaLabelledBy="grm-title"
    closeOnBackdrop={false}
    closeOnEscape={!state.restarting}
    onClose={() => gatewayFreshness.dismiss()}
  >
    {#snippet header()}
      <div class="grm-header">
        <img class="grm-logo" src="/logo-512.png" alt="" aria-hidden="true" />
        <div>
          <h3 id="grm-title">{MODAL_TITLE}</h3>
          <span class="grm-tag">Model gateway · port {report.port ?? '—'}</span>
        </div>
      </div>
    {/snippet}
    {#snippet body()}
      <p class="grm-message">{MODAL_MESSAGE}</p>
      {#if versionLine}
        <p class="grm-detail">{versionLine}</p>
      {/if}
      <p class="grm-detail grm-mono">{report.summary}</p>

      {#if !restartable && !state.result}
        <div class="grm-note">
          It cannot be restarted from here: {report.restart?.reason ??
            'no service manager owns the running gateway.'}
        </div>
      {/if}

      {#if state.restarting}
        <div class="grm-progress" role="status">
          <span class="grm-spinner" aria-hidden="true"></span>
          Restarting the gateway and checking it serves the updated version…
        </div>
      {/if}

      {#if state.result}
        <div class="grm-result" class:grm-result-ok={state.result.restarted} role="status">
          {state.result.message}
        </div>
      {/if}

      {#if state.error}
        <div class="grm-error" role="alert">Restart failed: {state.error}</div>
      {/if}
    {/snippet}
    {#snippet footer()}
      <div class="grm-footer">
        {#if state.result}
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={() => gatewayFreshness.dismiss()}>
            Close
          </button>
        {:else}
          <button
            class="btn-3d btn-3d-ghost btn-3d-sm"
            disabled={state.restarting}
            onclick={() => gatewayFreshness.dismiss()}
          >
            Dismiss
          </button>
          {#if restartable}
            <button
              class="btn-3d btn-3d-primary btn-3d-sm"
              disabled={state.restarting}
              onclick={() => gatewayFreshness.continueRestart()}
            >
              {state.restarting ? 'Restarting…' : 'Continue'}
            </button>
          {/if}
        {/if}
      </div>
    {/snippet}
  </DialogRoot>
{/if}

<style>
  .grm-header {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .grm-logo {
    width: 36px;
    height: 36px;
  }
  .grm-header h3 {
    margin: 0;
    font-size: 16px;
    font-weight: 700;
    color: var(--color-text);
  }
  .grm-tag {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--color-teal);
  }
  .grm-message {
    margin: 0 0 10px;
    font-size: 13px;
    line-height: 1.6;
    color: var(--color-text);
  }
  .grm-detail {
    margin: 0 0 6px;
    font-size: 12px;
    line-height: 1.6;
    color: var(--color-mid);
  }
  .grm-mono {
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    font-size: 11px;
    word-break: break-word;
  }
  .grm-note {
    margin-top: 12px;
    padding: 10px 12px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--color-mid);
    background: rgba(123, 95, 255, 0.08);
    border: 1px solid rgba(123, 95, 255, 0.3);
    border-radius: 10px;
  }
  .grm-progress {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 12px;
    font-size: 12px;
    color: var(--color-mid);
  }
  .grm-spinner {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2px solid rgba(0, 191, 166, 0.25);
    border-top-color: var(--color-teal);
    animation: grm-spin 0.9s linear infinite;
  }
  @keyframes grm-spin {
    to {
      transform: rotate(360deg);
    }
  }
  .grm-result {
    margin-top: 12px;
    padding: 10px 12px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--color-border);
    border-radius: 10px;
  }
  .grm-result-ok {
    background: rgba(0, 191, 166, 0.08);
    border-color: rgba(0, 191, 166, 0.35);
  }
  .grm-error {
    margin-top: 12px;
    padding: 10px 12px;
    font-size: 12px;
    color: var(--color-pink);
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 10px;
  }
  .grm-footer {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }
</style>
