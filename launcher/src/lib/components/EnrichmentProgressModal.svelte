<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.18 (Commit 9): progress modal for the embedding-enrichment
  // migration that fires when the user changes a project's KG /
  // codegraph embedding model in the dropdown.
  //
  // v0.2.18 follow-up (Commit 10): extended to drive enrichment
  // sequentially across MULTIPLE sibling collections in a single run.
  // The codegraph Save path enrich-migrates 5 sibling Code* classes
  // (CodeModule/CodeClass/CodeFunction/CodeAPI/CodeInteraction); the
  // KG Save path enrich-migrates exactly 1 collection. The modal
  // accepts a `collections` list and runs them one after the other.
  // Soft-fail per collection — one bad class doesn't abort the
  // remainder (partial enrichment is still useful for the others).
  //
  // States this modal renders:
  //   1. `running` — at least one collection's enrichment in flight.
  //      Header shows "Enriching N / M: <name>". The per-class progress
  //      bar reflects intra-class progress; the aggregate footer
  //      shows total enriched across all completed classes so far.
  //   2. `complete` — every collection's enrichment resolved (success
  //      or per-class error). Renders the aggregate report: per-class
  //      enriched / skipped / failed line + a totals row. Close button
  //      enabled.
  //   3. `error` — pre-flight error path on the FIRST collection. We
  //      stay in this state for the first-collection-failure case
  //      because the failure mode is usually "schema not migrated"
  //      which applies to ALL siblings; surfacing it once with the
  //      remediation hint is more useful than fanning the same error
  //      across 5 retries. Subsequent per-class errors during a
  //      multi-class run are captured in the per-class status list
  //      and the run proceeds.
  //
  // Caller contract:
  //   <EnrichmentProgressModal
  //     collections={[{name: 'MyProj_CodeFunction', new_slot: 'jina_embed'},
  //                   {name: 'MyProj_CodeClass',    new_slot: 'jina_embed'},
  //                   ...]}
  //     projectId={projectId}
  //     dryRun={false}
  //     onClose={() => (showModal = false)}
  //   />
  //   The modal kicks off the enrichment loop automatically on mount;
  //   onClose fires after the user dismisses any final-state UI.

  import { onDestroy, onMount, untrack } from 'svelte';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { invoke, listen } from '$lib/tauri';
  import type {
    EnrichmentReport,
    EnrichmentProgress,
  } from '$lib/types/embedding-enrichment';

  type CollectionTarget = { name: string; new_slot: string };

  let {
    collections,
    projectId,
    dryRun = false,
    onClose,
  }: {
    collections: CollectionTarget[];
    projectId: string | null;
    dryRun?: boolean;
    onClose: () => void;
  } = $props();

  // ─── per-collection state ──────────────────────────────────────────
  // One row per input collection. `status` tracks where each is in the
  // sequential pipeline so the UI can render checkmarks / pending dots /
  // error lines without losing track when the next class starts.
  type PerClassStatus = 'pending' | 'running' | 'done' | 'error';
  type PerClassRow = {
    name: string;
    new_slot: string;
    status: PerClassStatus;
    report: EnrichmentReport | null;
    error: string | null;
  };

  let rows = $state<PerClassRow[]>(
    untrack(() =>
      collections.map((c) => ({
        name: c.name,
        new_slot: c.new_slot,
        status: 'pending' as PerClassStatus,
        report: null,
        error: null,
      })),
    ),
  );

  // Which collection is currently being processed (index into `rows`).
  // Driven forward by the sequential runner in onMount.
  let currentIndex = $state(0);

  // Intra-class progress in [0, 1]. Reset to 0 at the start of each
  // collection; the listener filters by name + slot so events from a
  // prior collection don't bleed into the current bar.
  let intraProgress = $state(0);
  let intraMessage = $state('Starting enrichment…');

  // Top-level phase. `running` while ANY row hasn't reached a terminal
  // state. `complete` once all rows are done/error. `error` is used
  // ONLY for the first-collection pre-flight failure path (see header
  // doc) — per-class errors mid-run keep the phase at `running` until
  // every row has terminated.
  type Phase = 'running' | 'complete' | 'error';
  let phase = $state<Phase>('running');

  // First-collection pre-flight error text (for the `error` phase
  // remediation panel). Null in the multi-class-soft-fail path.
  let preflightError = $state<string | null>(null);

  // Tauri unsubscriber — released in onDestroy.
  let unlisten: (() => void) | null = null;

  // ─── lifecycle ─────────────────────────────────────────────────────
  onMount(async () => {
    // Subscribe to progress events BEFORE invoking the FIRST command so
    // we don't miss its first batch's emit. The subscription stays
    // alive across all 5 collections — the listener payload filter
    // (collection + slot) ensures only the in-flight row's events
    // update the local progress bar.
    try {
      unlisten = await listen<EnrichmentProgress>(
        'vct-enrichment-progress',
        (e) => {
          const p = e.payload;
          if (!p) return;
          const current = rows[currentIndex];
          if (!current) return;
          if (p.collection !== current.name || p.new_slot !== current.new_slot) {
            return;
          }
          intraProgress = Math.max(0, Math.min(1, p.progress));
          if (p.message) intraMessage = p.message;
        },
      );
    } catch (e) {
      // Listening failed — non-fatal, the modal just won't show a
      // running progress bar. The invokes below still run.
      console.warn('[vct] enrichment progress listener failed', e);
    }

    // Sequential runner: enrich one collection at a time. Soft-fail
    // per collection — a bad class records its error and proceeds to
    // the next. Exception: a first-collection pre-flight failure
    // surfaces the dedicated `error` remediation panel because the
    // most common cause (schema not migrated) is class-agnostic.
    for (let i = 0; i < rows.length; i++) {
      currentIndex = i;
      intraProgress = 0;
      intraMessage = `Starting ${rows[i].name}…`;
      rows[i].status = 'running';
      // Trigger reactivity — Svelte 5 detects nested mutations on
      // $state arrays, but reassigning the row makes the dependency
      // explicit (handy for the per-class status list re-render).
      rows = rows;

      try {
        const r = await invoke<EnrichmentReport>(
          'enrich_collection_vectors',
          {
            collectionName: rows[i].name,
            newSlot: rows[i].new_slot,
            projectId,
            dryRun,
          },
        );
        rows[i].report = r;
        rows[i].status = 'done';
        intraProgress = 1;
        intraMessage = formatDoneMessage(r, dryRun);
      } catch (e) {
        const msg = String(e);
        rows[i].error = msg;
        rows[i].status = 'error';
        // First-collection pre-flight failure → switch to the
        // `error` phase IF no later collection has been attempted.
        // We still continue the loop so siblings get a fair shot in
        // case the error was class-specific (rare but possible —
        // e.g. one Code* class was never seeded).
        if (i === 0 && rows.length > 1) {
          preflightError = msg;
        } else if (rows.length === 1) {
          // Single-collection caller (KG Save path). Surface the
          // error panel like the legacy single-collection modal did.
          preflightError = msg;
          phase = 'error';
          rows = rows;
          return;
        }
      }
      rows = rows;
    }

    // All rows terminated. Decide final phase:
    // - If `preflightError` was set on the very first attempt AND
    //   every subsequent row also failed → stay in `error` phase
    //   (the user needs the remediation hint, not a useless summary).
    // - Otherwise → `complete`, render the aggregate report.
    const everyFailed = rows.every((r) => r.status === 'error');
    if (preflightError && everyFailed) {
      phase = 'error';
    } else {
      phase = 'complete';
      preflightError = null;
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

  const intraPctText = $derived(
    `${(Math.min(1, intraProgress) * 100).toFixed(1)}%`,
  );

  // Aggregate counters across all completed rows (running row's
  // intra-class numbers are NOT included here — they would double-
  // count once the row finishes).
  const aggregate = $derived.by(() => {
    let total = 0;
    let enriched = 0;
    let skipped = 0;
    let failed = 0;
    let completedRows = 0;
    for (const r of rows) {
      if (r.status === 'done' && r.report) {
        total += r.report.total;
        enriched += r.report.enriched;
        skipped += r.report.skipped;
        failed += r.report.failed;
        completedRows += 1;
      } else if (r.status === 'error') {
        completedRows += 1;
      }
    }
    return { total, enriched, skipped, failed, completedRows };
  });

  // The user-facing "N / M" — 1-indexed for humans. While running
  // we use currentIndex+1; on complete we use rows.length (we're
  // past the last row).
  const collectionsLabel = $derived.by(() => {
    if (phase === 'complete' || phase === 'error') {
      return `${aggregate.completedRows} / ${rows.length}`;
    }
    return `${Math.min(currentIndex + 1, rows.length)} / ${rows.length}`;
  });

  // Per-collection summary for the done-state table. Excludes the
  // dry-run sentinel from each row's failure list.
  function rowFailures(row: PerClassRow) {
    return (row.report?.failures ?? []).filter(
      (f) => !('dry_run_count' in f),
    );
  }

  // Whether the run is finished (modal can be safely closed).
  const allDone = $derived(
    rows.every((r) => r.status === 'done' || r.status === 'error'),
  );

  // Determine if there are any captured per-class errors to surface
  // in the complete-state summary (independent of the `error` phase).
  const hasPerClassErrors = $derived(
    rows.some((r) => r.status === 'error'),
  );
</script>

<DialogRoot open={true} width="640px" onClose={allDone ? onClose : undefined}>
  {#snippet header()}
    <div class="epm-header">
      {#if phase === 'error'}
        <h3>Enrichment couldn’t start</h3>
      {:else if dryRun}
        <h3>Dry-run: enrichment preview</h3>
      {:else if phase === 'complete'}
        <h3>Enrichment complete</h3>
      {:else}
        <h3>
          Enriching collection {collectionsLabel}: <code>{rows[currentIndex]?.name ?? ''}</code>
        </h3>
      {/if}
      {#if phase !== 'error' && rows[currentIndex]}
        <p>
          <span>Slot:</span> <code>{rows[currentIndex].new_slot}</code>
          {#if rows.length > 1}
            <span> · Sweep:</span> {collectionsLabel} sibling classes
          {/if}
        </p>
      {/if}
    </div>
  {/snippet}
  {#snippet body()}
    {#if phase === 'error'}
      <section class="epm-error-box">
        <p class="epm-error-text">{preflightError}</p>
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
        {#if phase !== 'complete'}
          <div class="epm-progress-track" aria-hidden="true">
            <div
              class="epm-progress-fill"
              style:width={intraPctText}
            ></div>
          </div>
          <p class="epm-progress-text" aria-live="polite">{intraMessage}</p>
        {/if}

        {#if rows.length > 1 || phase === 'complete'}
          <ul class="epm-class-list">
            {#each rows as row, i (row.name)}
              <li class="epm-class-row" class:epm-class-active={i === currentIndex && phase === 'running'}>
                <span class="epm-class-status epm-class-status-{row.status}">
                  {#if row.status === 'done'}✓{:else if row.status === 'error'}✗{:else if row.status === 'running'}…{:else}·{/if}
                </span>
                <code class="epm-class-name">{row.name}</code>
                <span class="epm-class-detail">
                  {#if row.status === 'done' && row.report}
                    {row.report.enriched} enriched, {row.report.skipped} skipped{#if row.report.failed > 0}, {row.report.failed} failed{/if}
                  {:else if row.status === 'error'}
                    <span class="epm-class-err">error: {row.error}</span>
                  {:else if row.status === 'running'}
                    in progress…
                  {:else}
                    pending
                  {/if}
                </span>
              </li>
            {/each}
          </ul>
        {/if}

        {#if phase === 'complete'}
          <div class="epm-summary">
            <div class="epm-stat">
              <strong>{aggregate.enriched}</strong>
              <span>enriched</span>
            </div>
            <div class="epm-stat">
              <strong>{aggregate.skipped}</strong>
              <span>skipped</span>
            </div>
            <div class="epm-stat" class:epm-stat-bad={aggregate.failed > 0 || hasPerClassErrors}>
              <strong>{aggregate.failed}</strong>
              <span>failed</span>
            </div>
            <div class="epm-stat">
              <strong>{aggregate.total}</strong>
              <span>total</span>
            </div>
          </div>

          {#if hasPerClassErrors}
            <p class="epm-class-errors-note">
              Some sibling classes errored out — see the list above.
              Re-running Save is idempotent and will retry the failed
              classes from scratch.
            </p>
          {/if}

          {#each rows as row (row.name + '-failures')}
            {#if row.status === 'done' && rowFailures(row).length > 0}
              <details class="epm-failures-block">
                <summary>
                  <code>{row.name}</code>: show first {rowFailures(row).length} failure{rowFailures(row).length === 1 ? '' : 's'}
                  {#if row.report && row.report.failed > rowFailures(row).length}
                    (of {row.report.failed} total — details capped at 20)
                  {/if}
                </summary>
                <ul class="epm-failures-list">
                  {#each rowFailures(row) as f (f.uuid ?? Math.random())}
                    <li>
                      <code>{f.uuid ?? '(unknown)'}</code>
                      <span class="epm-failure-error">{f.error}</span>
                    </li>
                  {/each}
                </ul>
              </details>
            {/if}
          {/each}
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
  .epm-progress-text {
    font-size: 12px;
    color: #ccc;
    margin: 0 0 12px;
    font-family: ui-monospace, monospace;
  }
  .epm-class-list {
    list-style: none;
    padding: 0;
    margin: 4px 0 12px;
    background: rgba(255,255,255,0.03);
    border-radius: 4px;
  }
  .epm-class-row {
    display: grid;
    grid-template-columns: 24px 1fr 1.4fr;
    gap: 8px;
    padding: 6px 10px;
    font-size: 12px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    align-items: center;
  }
  .epm-class-row:last-child { border-bottom: none; }
  .epm-class-active {
    background: rgba(0,191,166,0.06);
  }
  .epm-class-status {
    font-family: ui-monospace, monospace;
    font-size: 13px;
    text-align: center;
    color: #666;
  }
  .epm-class-status-done { color: rgb(0,191,166); }
  .epm-class-status-running { color: rgb(255,200,80); }
  .epm-class-status-error { color: #f99; }
  .epm-class-status-pending { color: #666; }
  .epm-class-name {
    font-family: ui-monospace, monospace;
    color: #c4b3ff;
    background: rgba(255,255,255,0.04);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .epm-class-detail {
    color: #aaa;
    font-size: 11px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .epm-class-err {
    color: #f99;
    font-family: ui-monospace, monospace;
  }
  .epm-class-errors-note {
    margin: 8px 0;
    padding: 8px 12px;
    background: rgba(255,200,80,0.06);
    border-left: 2px solid rgba(255,200,80,0.4);
    font-size: 11px;
    color: #ccc;
    border-radius: 4px;
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
