<script lang="ts">
  // Defect B (v0.2.68): shared PRESENTATIONAL progress banner for a
  // long-running background OPERATION (project setup today; module install
  // later). Driven entirely by a normalized view-model — no store coupling.
  //
  // v0.2.95 R7: the banner chrome it used to carry inline (an `.op-*` clone
  // of the three `.bg-*` sync banners) now comes from the shared
  // `StatusBannerShell.svelte`, so all four banners are one family with one
  // palette. This file keeps the operation-specific parts: the view-model
  // contract, the Retry/Dismiss affordances, and the warnings drawer.
  //
  // The four statuses map to the impatient-user UX directive:
  //   running  → teal, spinner, plain-language phase label + elapsed timer +
  //              reassurance copy "Project saved — setup finishes in the
  //              background".
  //   deferred → amber, INFORMATIONAL (no Retry) — e.g. "Knowledge collections
  //              will be created when Weaviate is ready".
  //   done     → green, auto-hides.
  //   failed   → red/pink, Retry button + error detail.

  import StatusBannerShell from './StatusBannerShell.svelte';
  import { toneForProjectSetupStatus } from './status-banner-tone';

  interface ViewModel {
    /** Prominent operation title — shows the PROJECT NAME ("Setting up
     *  <name>"). */
    title: string;
    /** Plain-language phase label ("installing bundle…" → "creating knowledge
     *  collections…" → "indexing (continues in the background)…"). */
    phaseLabel: string;
    status: 'running' | 'deferred' | 'done' | 'failed';
    /** Optional secondary detail line (elapsed, reassurance, queue count). */
    detail?: string;
    /** Failure message (status='failed' only). */
    error?: string | null;
    /** Classified warnings to list in the terminal state. */
    warnings?: { message: string; severity: 'info' | 'error' }[];
    /** Retry handler (status='failed' only). When absent, no Retry button. */
    onRetry?: (() => void) | null;
    /** Dismiss handler (terminal states). When absent, no dismiss affordance. */
    onDismiss?: (() => void) | null;
  }

  let { vm }: { vm: ViewModel | null } = $props();

  let expanded = $state(false);
  let retrying = $state(false);

  function statusGlyph(s: ViewModel['status']): string {
    switch (s) {
      case 'running': return '⟳';
      case 'deferred': return 'ℹ';
      case 'done': return '✓';
      case 'failed': return '!';
    }
  }

  async function handleRetry() {
    if (retrying || !vm?.onRetry) return;
    retrying = true;
    try {
      await vm.onRetry();
      expanded = false;
    } finally {
      retrying = false;
    }
  }
</script>

{#if vm}
  <StatusBannerShell
    tone={toneForProjectSetupStatus(vm.status)}
    glyph={statusGlyph(vm.status)}
    spinning={vm.status === 'running'}
    title={vm.title}
    strongTitle
    phase={vm.phaseLabel}
    detail={vm.detail}
    alert={vm.status === 'failed'}
    showExpanded={expanded && (vm.warnings?.length ?? 0) > 0}
    expandedLabel="Setup details"
    expandedRole="group"
  >
    {#snippet actions()}
      {#if (vm?.warnings?.length ?? 0) > 0}
        <button
          type="button"
          class="bg-btn-secondary"
          onclick={() => (expanded = !expanded)}
          aria-expanded={expanded}
        >{expanded ? 'Hide details' : 'Show details'}</button>
      {/if}
      {#if vm?.status === 'failed' && vm.onRetry}
        <button
          type="button"
          class="bg-btn-primary"
          onclick={handleRetry}
          disabled={retrying}
        >{retrying ? 'Retrying…' : 'Retry'}</button>
      {/if}
      {#if (vm?.status === 'done' || vm?.status === 'deferred') && vm.onDismiss}
        <button
          type="button"
          class="bg-btn-x"
          aria-label="Dismiss banner"
          onclick={vm.onDismiss}
        >×</button>
      {/if}
    {/snippet}

    {#snippet expandedContent()}
      {#if vm?.error}
        <div class="bg-expand-row">
          <strong>Error</strong>
          <pre class="bg-pre">{vm.error}</pre>
        </div>
      {/if}
      <ul class="op-warn-list">
        {#each vm?.warnings ?? [] as w}
          <li class="op-warn op-warn-{w.severity}">{w.message}</li>
        {/each}
      </ul>
    {/snippet}
  </StatusBannerShell>
{/if}

<style>
  /* Operation-specific only — the banner chrome lives in
     StatusBannerShell.svelte. The F5 severity split: amber for
     informational warnings (a deferral, preserved files), pink for a real
     subprocess failure. */
  .op-warn-list { margin: 0; padding-left: 18px; }
  .op-warn { margin: 2px 0; }
  .op-warn-info { color: rgb(245, 179, 66); }
  .op-warn-error { color: rgb(255, 130, 180); }
</style>
