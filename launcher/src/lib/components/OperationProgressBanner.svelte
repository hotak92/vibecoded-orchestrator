<script lang="ts">
  // Defect B (v0.2.68): shared PRESENTATIONAL progress banner.
  //
  // Driven entirely by a normalized view-model — it has NO store coupling, so
  // it can be fed by an adapter from the `project-setup` store (this task) and
  // later reused by module-install. Brand style (navy + teal #00BFA6, glass
  // card) per `.claude/references/VCO_BRAND_REFERENCE.md`. Status-row layout +
  // tinting mirror `CodeGraphBuildBanner.svelte`; the aria semantics
  // (`role="status"` aria-live="polite", upgraded to `role="alert"` on
  // failure) are copied from CodeGraphBuildBanner:185-186 verbatim.
  //
  // The four statuses map to the impatient-user UX directive:
  //   running  → teal, spinner, plain-language phase label + elapsed timer +
  //              reassurance copy "Project saved — setup finishes in the
  //              background".
  //   deferred → amber, INFORMATIONAL (no Retry) — e.g. "Knowledge collections
  //              will be created when Weaviate is ready".
  //   done     → green, auto-hides.
  //   failed   → red/pink, Retry button + error detail.

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
  <div
    class="op-banner status-{vm.status}"
    role={vm.status === 'failed' ? 'alert' : 'status'}
    aria-live="polite"
  >
    <div class="op-row">
      <span
        class="op-glyph"
        class:spin={vm.status === 'running'}
        aria-hidden="true"
      >{statusGlyph(vm.status)}</span>
      <div class="op-text">
        <div class="op-title">{vm.title}</div>
        <div class="op-phase">{vm.phaseLabel}</div>
        {#if vm.detail}
          <div class="op-detail">{vm.detail}</div>
        {/if}
      </div>
      <div class="op-actions">
        {#if (vm.warnings?.length ?? 0) > 0}
          <button
            type="button"
            class="op-btn-secondary"
            onclick={() => (expanded = !expanded)}
            aria-expanded={expanded}
          >{expanded ? 'Hide details' : 'Show details'}</button>
        {/if}
        {#if vm.status === 'failed' && vm.onRetry}
          <button
            type="button"
            class="op-btn-primary"
            onclick={handleRetry}
            disabled={retrying}
          >{retrying ? 'Retrying…' : 'Retry'}</button>
        {/if}
        {#if (vm.status === 'done' || vm.status === 'deferred') && vm.onDismiss}
          <button
            type="button"
            class="op-btn-x"
            aria-label="Dismiss banner"
            onclick={vm.onDismiss}
          >×</button>
        {/if}
      </div>
    </div>

    {#if expanded && (vm.warnings?.length ?? 0) > 0}
      <div class="op-expand" role="group" aria-label="Setup details">
        {#if vm.error}
          <div class="op-expand-row">
            <strong>Error</strong>
            <pre class="op-pre">{vm.error}</pre>
          </div>
        {/if}
        <ul class="op-warn-list">
          {#each vm.warnings ?? [] as w}
            <li class="op-warn op-warn-{w.severity}">{w.message}</li>
          {/each}
        </ul>
      </div>
    {/if}
  </div>
{/if}

<style>
  /* Brand: navy surface, teal accent (#00BFA6), glass card. Layout mirrors
     CodeGraphBuildBanner so the two banner generations stay consistent. */
  .op-banner {
    display: block;
    border-bottom: 1px solid transparent;
    font-size: 13px;
    line-height: 1.4;
    backdrop-filter: blur(8px);
  }
  .op-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 24px;
  }
  .op-glyph {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    font-family: ui-monospace, monospace;
    font-size: 12px;
    font-weight: 600;
    flex-shrink: 0;
  }
  .op-glyph.spin { animation: op-spin 1.4s linear infinite; }
  @keyframes op-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  .op-text { flex: 1; min-width: 0; }
  /* Project name is the prominent line (impatient-user directive). */
  .op-title { font-weight: 700; }
  .op-phase { font-size: 12.5px; margin-top: 1px; }
  .op-detail { font-size: 11.5px; color: rgba(255,255,255,0.55); margin-top: 2px; }
  .op-actions { display: flex; gap: 8px; flex-shrink: 0; align-items: center; }

  .op-btn-secondary, .op-btn-primary {
    padding: 4px 12px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
    font-family: inherit;
  }
  .op-btn-secondary {
    background: rgba(255,255,255,0.06);
    color: #ccc;
    border-color: rgba(255,255,255,0.12);
  }
  .op-btn-secondary:hover { background: rgba(255,255,255,0.1); }
  .op-btn-primary {
    background: rgb(0,191,166);
    color: #001a17;
  }
  .op-btn-primary:hover:not(:disabled) { background: rgb(0,210,180); }
  .op-btn-primary:disabled { opacity: 0.5; cursor: default; }
  .op-btn-x {
    background: none; border: none; color: inherit;
    font-size: 18px; line-height: 1; cursor: pointer;
    padding: 0 8px; border-radius: 6px;
    opacity: 0.6;
  }
  .op-btn-x:hover { opacity: 1; background: rgba(255,255,255,0.06); }

  .op-expand {
    padding: 8px 24px 14px 56px;
    font-size: 12px;
    border-top: 1px dashed rgba(255,255,255,0.08);
  }
  .op-expand-row strong {
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: rgba(255,255,255,0.5);
    margin-bottom: 4px;
  }
  .op-pre {
    margin: 0 0 8px 0;
    padding: 6px 8px;
    background: rgba(255,255,255,0.04);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 11px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 200px;
    overflow-y: auto;
    color: rgba(255,255,255,0.85);
  }
  .op-warn-list { margin: 0; padding-left: 18px; }
  .op-warn { margin: 2px 0; }
  .op-warn-info { color: rgb(245, 179, 66); }
  .op-warn-error { color: rgb(255, 130, 180); }

  /* Per-status tinting — teal running, amber deferred (informational),
     green done, pink failed. Cloned from CodeGraphBuildBanner palette. */
  .status-running {
    background: rgba(0,191,166,0.08);
    border-bottom-color: rgba(0,191,166,0.30);
    color: rgb(0,191,166);
  }
  .status-running .op-glyph {
    background: rgba(0,191,166,0.15);
    color: rgb(0,191,166);
  }
  .status-deferred {
    background: rgba(245, 179, 66, 0.08);
    border-bottom-color: rgba(245, 179, 66, 0.30);
    color: rgb(245, 179, 66);
  }
  .status-deferred .op-glyph {
    background: rgba(245, 179, 66, 0.18);
    color: rgb(245, 179, 66);
  }
  .status-done {
    background: rgba(70, 200, 120, 0.08);
    border-bottom-color: rgba(70, 200, 120, 0.30);
    color: rgb(120, 220, 160);
  }
  .status-done .op-glyph {
    background: rgba(70, 200, 120, 0.18);
    color: rgb(120, 220, 160);
  }
  .status-failed {
    background: rgba(255, 79, 160, 0.10);
    border-bottom-color: rgba(255, 79, 160, 0.35);
    color: rgb(255, 130, 180);
  }
  .status-failed .op-glyph {
    background: rgba(255, 79, 160, 0.18);
    color: rgb(255, 130, 180);
  }
</style>
