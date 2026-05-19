<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.18 (Plan C): Re-analyze code-graph progress modal.
  //
  // Forks `EnrichmentProgressModal` for the analyze_code_graph.py
  // re-walk that Plan C wires onto the user-clickable "Re-analyze code
  // graph" button. Differences vs the enrichment modal:
  //
  //   * Subscribes to `vct-reanalysis-progress` (not -enrichment-).
  //   * Calls the Tauri command `reanalyze_code_graph` (not
  //     `enrich_collection_vectors`).
  //   * Renders per-file analyzer progress + the final summary (files
  //     analyzed, classes, functions, APIs, stale pruned) rather than
  //     the enrichment counters.
  //
  // Caller contract:
  //   <CodeGraphReanalysisModal
  //     projectId={projectId}
  //     projectName={projectName}
  //     language={null}                  // or "python" / "go" / …
  //     onClose={() => (showModal = false)}
  //   />
  //   The modal kicks off the re-analysis on mount; onClose fires after
  //   the user dismisses any final-state UI.

  import { onDestroy, onMount } from 'svelte';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { invoke, listen } from '$lib/tauri';
  import type {
    ReanalysisReport,
    ReanalysisProgress,
  } from '$lib/types/codegraph-reanalysis';

  let {
    projectId,
    projectName,
    language = null,
    onClose,
  }: {
    projectId: string;
    projectName: string;
    language?: string | null;
    onClose: () => void;
  } = $props();

  // ─── state ─────────────────────────────────────────────────────────
  type Phase = 'running' | 'complete' | 'error';

  let phase = $state<Phase>('running');
  let progress = $state(0);
  let message = $state('Starting re-analysis…');
  let currentFile = $state<string>('');
  let report = $state<ReanalysisReport | null>(null);
  let errorText = $state<string | null>(null);

  // Tauri unsubscriber — released in onDestroy.
  let unlisten: (() => void) | null = null;

  // ─── lifecycle ─────────────────────────────────────────────────────
  onMount(async () => {
    // Subscribe BEFORE invoking the command so we don't miss the first
    // batch's emit (Tauri events are not buffered).
    try {
      unlisten = await listen<ReanalysisProgress>(
        'vct-reanalysis-progress',
        (e) => {
          const p = e.payload;
          if (!p) return;
          // Filter by project name so a different re-analysis running
          // in parallel doesn't cross-pollute this modal.
          if (p.project !== projectName) return;
          progress = Math.max(0, Math.min(1, p.progress));
          if (p.message) message = p.message;
          if (p.file) currentFile = p.file;
        },
      );
    } catch (e) {
      // Listening failed — non-fatal, the modal just won't show a
      // per-file progress bar; the invoke() below still runs.
      console.warn('[vct] reanalysis progress listener failed', e);
    }

    try {
      const r = await invoke<ReanalysisReport>('reanalyze_code_graph', {
        projectId,
        language: language ?? null,
      });
      report = r;
      progress = 1;
      message = formatDoneMessage(r);
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

  function formatDoneMessage(r: ReanalysisReport): string {
    const langPart = r.language ? ` (${r.language} only)` : '';
    let msg = `Analyzed ${r.files_analyzed} files${langPart}`;
    if (r.stale_pruned > 0) msg += `, pruned ${r.stale_pruned} stale`;
    if (r.insert_errors > 0) msg += `, ${r.insert_errors} insert error(s)`;
    return msg;
  }

  // ─── derived ───────────────────────────────────────────────────────
  const cancelTooltip =
    'Cancellation isn’t supported. The analyzer is idempotent — close ' +
    'this modal and re-run from the button to continue from where it ' +
    'left off (unchanged files are skipped via file-hash).';

  const pctText = $derived(
    `${(Math.min(1, progress) * 100).toFixed(1)}%`,
  );

  const headerSubtitle = $derived(
    language
      ? `Language-scoped: ${language}`
      : 'Full multi-language re-walk',
  );
</script>

<DialogRoot open={true} width="560px" onClose={onClose}>
  {#snippet header()}
    <div class="cgr-header">
      {#if phase === 'error'}
        <h3>Re-analyze failed</h3>
      {:else}
        <h3>
          {phase === 'complete'
            ? 'Re-analysis complete'
            : 'Re-analyzing code graph…'}
        </h3>
      {/if}
      <p>
        <span>Project:</span> <code>{projectName}</code>
        <span> · </span> {headerSubtitle}
      </p>
    </div>
  {/snippet}
  {#snippet body()}
    {#if phase === 'error'}
      <section class="cgr-error-box">
        <p class="cgr-error-text">{errorText}</p>
        <div class="cgr-hint">
          <p>Common causes:</p>
          <ul>
            <li>
              <code>NoEmbeddingBackendError</code>: the relevant code
              backend (CodeEmbed / Ollama / OpenAI) isn’t reachable. Start
              the service then re-click Re-analyze.
            </li>
            <li>
              <code>schema case collision</code>: an existing Weaviate
              class has the same name modulo case. Rename one and
              re-run.
            </li>
            <li>
              <code>analyzer exit 3</code>: no files indexed — check that
              the project folder contains supported source files within
              depth 3.
            </li>
          </ul>
        </div>
      </section>
    {:else}
      <section class="cgr-section">
        <div class="cgr-progress-track" aria-hidden="true">
          <div
            class="cgr-progress-fill"
            style:width={pctText}
            class:complete={phase === 'complete'}
          ></div>
        </div>
        <p class="cgr-progress-text" aria-live="polite">{message}</p>
        {#if currentFile && phase === 'running'}
          <p class="cgr-current-file"><code>{currentFile}</code></p>
        {/if}

        {#if phase === 'complete' && report}
          <div class="cgr-summary">
            <div class="cgr-stat">
              <strong>{report.files_analyzed}</strong>
              <span>files</span>
            </div>
            <div class="cgr-stat">
              <strong>{report.classes}</strong>
              <span>classes</span>
            </div>
            <div class="cgr-stat">
              <strong>{report.functions}</strong>
              <span>functions</span>
            </div>
            <div class="cgr-stat">
              <strong>{report.apis}</strong>
              <span>APIs</span>
            </div>
            <div class="cgr-stat" class:cgr-stat-bad={report.insert_errors > 0}>
              <strong>{report.insert_errors}</strong>
              <span>errors</span>
            </div>
            <div class="cgr-stat" class:cgr-stat-good={report.stale_pruned > 0}>
              <strong>{report.stale_pruned}</strong>
              <span>pruned</span>
            </div>
          </div>
        {/if}
      </section>
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="cgr-footer">
      {#if phase === 'running'}
        <button
          class="cgr-btn"
          disabled
          title={cancelTooltip}
          aria-label="Cancel (disabled)"
        >
          Cancel (idempotent)
        </button>
      {:else}
        <button class="cgr-btn cgr-btn-primary" onclick={onClose}>
          Close
        </button>
      {/if}
    </div>
  {/snippet}
</DialogRoot>

<style>
  .cgr-header h3 { margin: 0; font-size: 14px; }
  .cgr-header p {
    margin: 6px 0 0;
    font-size: 11px;
    color: #aaa;
  }
  .cgr-header code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    color: #c4b3ff;
  }
  .cgr-section { margin-bottom: 8px; }
  .cgr-progress-track {
    width: 100%;
    height: 8px;
    background: rgba(255,255,255,0.06);
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 10px;
  }
  .cgr-progress-fill {
    height: 100%;
    background: rgba(0,191,166,0.8);
    transition: width 0.2s ease;
  }
  .cgr-progress-fill.complete { background: rgb(0,191,166); }
  .cgr-progress-text {
    font-size: 12px;
    color: #ccc;
    margin: 0 0 6px;
    font-family: ui-monospace, monospace;
  }
  .cgr-current-file {
    margin: 0 0 12px;
    font-size: 10px;
    color: #888;
  }
  .cgr-current-file code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.04);
    padding: 1px 4px;
    border-radius: 2px;
  }
  .cgr-summary {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 6px;
    margin: 12px 0;
  }
  .cgr-stat {
    background: rgba(255,255,255,0.04);
    padding: 6px 8px;
    border-radius: 4px;
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .cgr-stat strong {
    font-size: 16px;
    color: #ddd;
  }
  .cgr-stat span {
    font-size: 9px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .cgr-stat-bad strong { color: #f99; }
  .cgr-stat-good strong { color: rgb(0,191,166); }
  .cgr-error-box {
    background: rgba(255,99,99,0.06);
    border-left: 2px solid rgba(255,99,99,0.5);
    padding: 10px 14px;
    border-radius: 4px;
  }
  .cgr-error-text {
    margin: 0 0 10px;
    color: #f99;
    font-size: 12px;
    font-family: ui-monospace, monospace;
    word-break: break-word;
  }
  .cgr-hint {
    margin: 0;
    font-size: 11px;
    color: #aaa;
    line-height: 1.6;
  }
  .cgr-hint p { margin: 0 0 4px; }
  .cgr-hint ul {
    margin: 6px 0 0;
    padding-left: 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .cgr-hint code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 2px;
    color: #c4b3ff;
  }
  .cgr-footer {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }
  .cgr-btn {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .cgr-btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }
  .cgr-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .cgr-btn-primary {
    background: rgb(0,191,166);
    border-color: rgb(0,191,166);
    color: #000;
    font-weight: 600;
  }
  .cgr-btn-primary:hover:not(:disabled) {
    background: rgb(0,210,180);
  }
</style>
