<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.18 (Commit 9): progress modal for the embedding-enrichment
  // migration that fires when the user changes a project's KG /
  // codegraph embedding model in the dropdown.
  //
  // States this modal renders:
  //   1. `running` — initial state. Progress bar driven by Tauri events
  //      from `vct-enrichment-progress`. Close button hidden; the
  //      "Cancel" button is rendered but DISABLED (with an explanatory
  //      tooltip per the locked design decision — cancellation isn't
  //      supported because aborting mid-batch leaves partial state, and
  //      idempotency means closing + re-running is the safer UX).
  //   2. `complete` — the enrichment Tauri command resolved. Renders
  //      the EnrichmentReport summary (enriched / skipped / failed) +
  //      optional failure-details disclosure. Close button enabled.
  //   3. `error` — pre-flight error path
  //      (UnknownSlotError / SlotNotInSchemaError / CollectionNotFoundError
  //      / NoEmbeddingBackendError) bubbled from the Tauri command. The
  //      message includes the user-fixable next step (run
  //      migrate-collections, start Ollama, etc.). Close button enabled.
  //
  // Caller contract:
  //   <EnrichmentProgressModal
  //     collection={col}
  //     newSlot={slot}
  //     projectId={projectId}
  //     dryRun={false}
  //     onClose={() => (showModal = false)}
  //   />
  //   The modal kicks off the enrichment call automatically on mount;
  //   onClose fires after the user dismisses any final-state UI.

  import { onDestroy, onMount } from 'svelte';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { invoke, listen } from '$lib/tauri';
  import type {
    EnrichmentReport,
    EnrichmentProgress,
  } from '$lib/types/embedding-enrichment';

  let {
    collection,
    newSlot,
    projectId,
    dryRun = false,
    onClose,
  }: {
    collection: string;
    newSlot: string;
    projectId: string | null;
    dryRun?: boolean;
    onClose: () => void;
  } = $props();

  // ─── state ─────────────────────────────────────────────────────────
  type Phase = 'running' | 'complete' | 'error';

  let phase = $state<Phase>('running');
  let progress = $state(0);
  let message = $state('Starting enrichment…');
  let report = $state<EnrichmentReport | null>(null);
  let errorText = $state<string | null>(null);
  let showFailures = $state(false);

  // Tauri unsubscriber — released in onDestroy.
  let unlisten: (() => void) | null = null;

  // ─── lifecycle ─────────────────────────────────────────────────────
  onMount(async () => {
    // Subscribe to progress events BEFORE invoking the command so we
    // don't miss the first batch's emit.
    try {
      unlisten = await listen<EnrichmentProgress>(
        'vct-enrichment-progress',
        (e) => {
          // Filter by collection + slot so a different enrichment
          // running in parallel (highly unlikely but defensively
          // possible) doesn't cross-pollute this modal.
          const p = e.payload;
          if (!p) return;
          if (p.collection !== collection || p.new_slot !== newSlot) return;
          progress = Math.max(0, Math.min(1, p.progress));
          if (p.message) message = p.message;
        },
      );
    } catch (e) {
      // Listening failed — non-fatal, the modal just won't show a
      // running progress bar. The invoke() below still runs.
      console.warn('[vct] enrichment progress listener failed', e);
    }

    try {
      const r = await invoke<EnrichmentReport>(
        'enrich_collection_vectors',
        {
          collectionName: collection,
          newSlot,
          projectId,
          dryRun,
        },
      );
      report = r;
      progress = 1;
      message = formatDoneMessage(r, dryRun);
      phase = 'complete';
    } catch (e) {
      errorText = String(e);
      phase = 'error';
    }
  });

  onDestroy(() => {
    if (unlisten) {
      try {
        unlisten();
      } catch {
        // ignore
      }
    }
  });

  function formatDoneMessage(r: EnrichmentReport, dry: boolean): string {
    if (dry) {
      const wouldHave =
        (r.failures[0]?.dry_run_count ?? 0) as number;
      return `Dry run: ${wouldHave} of ${r.total} would be enriched`;
    }
    let msg = `Enriched ${r.enriched} of ${r.total}`;
    if (r.skipped > 0) msg += `, skipped ${r.skipped}`;
    if (r.failed > 0) msg += `, failed ${r.failed}`;
    return msg;
  }

  // ─── derived ───────────────────────────────────────────────────────
  const cancelTooltip =
    'Cancellation isn’t supported. The enrichment is idempotent — close ' +
    'this modal and re-run from the dropdown to continue from where it ' +
    'left off.';

  const pctText = $derived(
    `${(Math.min(1, progress) * 100).toFixed(1)}%`,
  );

  // Failures excluding the dry-run sentinel, for the disclosure list.
  const realFailures = $derived(
    (report?.failures ?? []).filter((f) => !('dry_run_count' in f)),
  );
</script>

<DialogRoot open={true} width="560px" onClose={onClose}>
  {#snippet header()}
    <div class="epm-header">
      {#if phase === 'error'}
        <h3>Enrichment couldn’t start</h3>
      {:else if dryRun}
        <h3>Dry-run: enrichment preview</h3>
      {:else}
        <h3>{phase === 'complete' ? 'Enrichment complete' : 'Computing embeddings…'}</h3>
      {/if}
      <p>
        <span>Collection:</span> <code>{collection}</code>
        <span> · Slot:</span> <code>{newSlot}</code>
      </p>
    </div>
  {/snippet}
  {#snippet body()}
    {#if phase === 'error'}
      <section class="epm-error-box">
        <p class="epm-error-text">{errorText}</p>
        <div class="epm-hint">
          <p>Common causes:</p>
          <ul>
            <li>
              <code>SlotNotInSchemaError</code>: the target slot isn’t in
              the live schema yet. Run
              <code>python -m vco_lib.project_init migrate-collections
              --name &lt;project&gt;</code> first.
            </li>
            <li>
              <code>NoEmbeddingBackendError</code>: the relevant backend
              (Ollama / CodeEmbed / OpenAI) isn’t reachable. Start the
              service or check your <code>OPENAI_API_KEY</code>.
            </li>
            <li>
              <code>CollectionNotFoundError</code>: the project’s KG /
              code graph hasn’t been seeded yet. Run the launcher’s
              “Seed KG” or “Rebuild code graph” action first.
            </li>
          </ul>
        </div>
      </section>
    {:else}
      <section class="epm-section">
        <div class="epm-progress-track" aria-hidden="true">
          <div
            class="epm-progress-fill"
            style:width={pctText}
            class:complete={phase === 'complete'}
          ></div>
        </div>
        <p class="epm-progress-text" aria-live="polite">{message}</p>

        {#if phase === 'complete' && report}
          <div class="epm-summary">
            <div class="epm-stat">
              <strong>{report.enriched}</strong>
              <span>enriched</span>
            </div>
            <div class="epm-stat">
              <strong>{report.skipped}</strong>
              <span>skipped</span>
            </div>
            <div class="epm-stat" class:epm-stat-bad={report.failed > 0}>
              <strong>{report.failed}</strong>
              <span>failed</span>
            </div>
            <div class="epm-stat">
              <strong>{report.total}</strong>
              <span>total</span>
            </div>
          </div>

          {#if realFailures.length > 0}
            <details
              class="epm-failures-block"
              bind:open={showFailures}
            >
              <summary>
                Show first {realFailures.length} failure{realFailures.length === 1 ? '' : 's'}
                {#if report.failed > realFailures.length}
                  (of {report.failed} total — details capped at 20)
                {/if}
              </summary>
              <ul class="epm-failures-list">
                {#each realFailures as f (f.uuid ?? Math.random())}
                  <li>
                    <code>{f.uuid ?? '(unknown)'}</code>
                    <span class="epm-failure-error">{f.error}</span>
                  </li>
                {/each}
              </ul>
            </details>
          {/if}
        {/if}
      </section>
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="epm-footer">
      {#if phase === 'running'}
        <button
          class="epm-btn"
          disabled
          title={cancelTooltip}
          aria-label="Cancel (disabled)"
        >
          Cancel (idempotent)
        </button>
      {:else}
        <button class="epm-btn epm-btn-primary" onclick={onClose}>
          Close
        </button>
      {/if}
    </div>
  {/snippet}
</DialogRoot>

<style>
  .epm-header h3 { margin: 0; font-size: 14px; }
  .epm-header p {
    margin: 6px 0 0;
    font-size: 11px;
    color: #aaa;
  }
  .epm-header code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    color: #c4b3ff;
  }
  .epm-section { margin-bottom: 8px; }
  .epm-progress-track {
    width: 100%;
    height: 8px;
    background: rgba(255,255,255,0.06);
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 10px;
  }
  .epm-progress-fill {
    height: 100%;
    background: rgba(0,191,166,0.8);
    transition: width 0.2s ease;
  }
  .epm-progress-fill.complete {
    background: rgb(0,191,166);
  }
  .epm-progress-text {
    font-size: 12px;
    color: #ccc;
    margin: 0 0 12px;
    font-family: ui-monospace, monospace;
  }
  .epm-summary {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    margin: 12px 0;
  }
  .epm-stat {
    background: rgba(255,255,255,0.04);
    padding: 8px 10px;
    border-radius: 4px;
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .epm-stat strong {
    font-size: 18px;
    color: #ddd;
  }
  .epm-stat span {
    font-size: 10px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .epm-stat-bad strong { color: #f99; }
  .epm-failures-block {
    margin-top: 8px;
    background: rgba(255,99,99,0.05);
    padding: 8px 12px;
    border-radius: 4px;
    border-left: 2px solid rgba(255,99,99,0.4);
  }
  .epm-failures-block summary {
    font-size: 11px;
    color: #f99;
    cursor: pointer;
    user-select: none;
  }
  .epm-failures-list {
    list-style: none;
    padding: 0;
    margin: 8px 0 0;
    max-height: 200px;
    overflow-y: auto;
  }
  .epm-failures-list li {
    font-size: 10px;
    padding: 4px 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .epm-failures-list code {
    font-family: ui-monospace, monospace;
    color: #ccc;
    background: rgba(255,255,255,0.04);
    padding: 1px 4px;
    border-radius: 2px;
    align-self: flex-start;
  }
  .epm-failure-error {
    color: #f99;
    font-family: ui-monospace, monospace;
    font-size: 10px;
  }
  .epm-error-box {
    background: rgba(255,99,99,0.06);
    border-left: 2px solid rgba(255,99,99,0.5);
    padding: 10px 14px;
    border-radius: 4px;
  }
  .epm-error-text {
    margin: 0 0 10px;
    color: #f99;
    font-size: 12px;
    font-family: ui-monospace, monospace;
    word-break: break-word;
  }
  .epm-hint {
    margin: 0;
    font-size: 11px;
    color: #aaa;
    line-height: 1.6;
  }
  .epm-hint p {
    margin: 0 0 4px;
  }
  .epm-hint ul {
    margin: 6px 0 0;
    padding-left: 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .epm-hint code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 2px;
    color: #c4b3ff;
  }
  .epm-footer {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }
  .epm-btn {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .epm-btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }
  .epm-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .epm-btn-primary {
    background: rgb(0,191,166);
    border-color: rgb(0,191,166);
    color: #000;
    font-weight: 600;
  }
  .epm-btn-primary:hover:not(:disabled) {
    background: rgb(0,210,180);
  }
</style>
