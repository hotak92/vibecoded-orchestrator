<script lang="ts">
  // 0.2.x backlog #4 (2026-05-10): Update-all-projects modal.
  //
  // Sequential update of every registered project. Shows a confirmation
  // step first (project count + "this will rerun the bundle install for
  // each"), then a live-progress phase where each project's status
  // transitions running → done / failed.
  //
  // Sequential — NOT fan-out: the backend iterates projects one at a
  // time. The UX rationale lives in `commands/projects_v2.rs` at the
  // ─── 0.2.x backlog #4 ─── header. While the iteration runs, the user
  // can't interrupt mid-list cleanly because the per-project work
  // (bundle install + schema bootstrap) is non-trivial to roll back.
  // Best we offer is "close this dialog after it finishes".

  import { projects } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import type {
    UpdateAllReport,
    UpdateAllProjectEntry,
  } from '$lib/types/launcher';

  let { open = $bindable<boolean>(false) }: { open: boolean } = $props();

  // Three phases:
  //   "confirm" — initial prompt + count of projects to update.
  //   "running" — Tauri call in flight. Spinner; no per-project rows
  //               yet because the backend doesn't stream events (one
  //               sync call returns the full report).
  //   "done"    — report received. Per-project rows rendered.
  let phase = $state<'confirm' | 'running' | 'done'>('confirm');
  let stopOnError = $state(true);
  let report = $state<UpdateAllReport | null>(null);
  let runError = $state<string | null>(null);

  const projectCount = $derived($projects.projects.length);

  // Reset state every time the modal opens. Without this, a second
  // "Update all" click would render the previous report's state until
  // the new run completes.
  $effect(() => {
    if (open) {
      phase = 'confirm';
      report = null;
      runError = null;
    }
  });

  async function runUpdateAll() {
    phase = 'running';
    runError = null;
    try {
      const r = await projects.updateAll({ stop_on_error: stopOnError });
      report = r;
      phase = 'done';

      // Surface a summary toast so the user sees the result even after
      // closing the modal.
      const parts: string[] = [];
      if (r.total_succeeded > 0) parts.push(`${r.total_succeeded} updated`);
      if (r.total_failed > 0) parts.push(`${r.total_failed} failed`);
      if (r.total_skipped > 0) parts.push(`${r.total_skipped} skipped`);
      const line = parts.length > 0 ? parts.join(', ') : 'no changes';
      if (r.total_failed > 0) {
        toast.error(`Update all finished: ${line}.`);
      } else {
        toast.success(`Update all finished: ${line}.`);
      }
    } catch (e) {
      runError = e instanceof Error ? e.message : String(e);
      phase = 'done';
      toast.error(`Update all failed: ${runError}`);
    }
  }

  function close() {
    open = false;
  }

  // Render-helpers for the per-row status indicator. Kept in the script
  // section so the markup stays readable; the icons are unicode so we
  // don't pull a font/icon dep just for three glyphs.
  function statusIcon(s: UpdateAllProjectEntry['status']): string {
    if (s === 'succeeded') return '✓';
    if (s === 'failed') return '✗';
    return '–';
  }
  function statusLabel(s: UpdateAllProjectEntry['status']): string {
    if (s === 'succeeded') return 'updated';
    if (s === 'failed') return 'failed';
    return 'skipped';
  }
</script>

<DialogRoot bind:open width="640px">
  {#snippet header()}
    <h3 class="ua-title">
      {#if phase === 'confirm'}Update all projects{/if}
      {#if phase === 'running'}Updating projects…{/if}
      {#if phase === 'done'}Update-all report{/if}
    </h3>
  {/snippet}

  {#snippet body()}
    {#if phase === 'confirm'}
      <p class="ua-desc">
        Re-runs the bundle install (hooks, scripts, agents, skills, infrastructure
        templates) on every registered project. User-modified files are preserved;
        only orchestrator-shipped files that match the prior installed hash are
        overwritten.
      </p>
      <p class="ua-count">
        <strong>{projectCount}</strong>
        {projectCount === 1 ? 'project' : 'projects'} will be updated, one at a time.
      </p>
      <label class="ua-toggle">
        <input type="checkbox" bind:checked={stopOnError} />
        <span>Stop at first failure (recommended)</span>
      </label>
      <p class="ua-hint">
        Unchecking continues past failures so every project is attempted. Failed
        projects appear in the report regardless.
      </p>
    {/if}

    {#if phase === 'running'}
      <div class="ua-running">
        <div class="ua-spinner" aria-label="Loading"></div>
        <p>Running bundle update for {projectCount} project{projectCount === 1 ? '' : 's'}…</p>
        <p class="ua-hint">
          This may take a minute. Each project runs in sequence; the modal will
          update when all done.
        </p>
      </div>
    {/if}

    {#if phase === 'done'}
      {#if runError}
        <div class="ua-error">
          <strong>Update-all failed:</strong>
          <pre>{runError}</pre>
        </div>
      {:else if report}
        <p class="ua-summary">
          <strong>{report.total_succeeded}</strong> updated,
          <strong>{report.total_failed}</strong> failed,
          <strong>{report.total_skipped}</strong> skipped.
        </p>
        <ul class="ua-rows">
          {#each report.updated as r (r.project_id)}
            <li class="ua-row ua-row-{r.status}">
              <span class="ua-row-icon">{statusIcon(r.status)}</span>
              <span class="ua-row-name">{r.project_name}</span>
              <span class="ua-row-status">{statusLabel(r.status)}</span>
              {#if r.error}
                <div class="ua-row-error" title={r.error}>{r.error}</div>
              {/if}
              {#if r.warnings.length > 0}
                <div class="ua-row-warnings">
                  {r.warnings.length} warning{r.warnings.length === 1 ? '' : 's'}
                </div>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  {/snippet}

  {#snippet footer()}
    {#if phase === 'confirm'}
      <button class="btn-ghost" onclick={close}>Cancel</button>
      <button class="btn-primary" onclick={runUpdateAll} disabled={projectCount === 0}>
        Update {projectCount} project{projectCount === 1 ? '' : 's'}
      </button>
    {:else if phase === 'running'}
      <button class="btn-ghost" disabled>Updating…</button>
    {:else}
      <button class="btn-primary" onclick={close}>Close</button>
    {/if}
  {/snippet}
</DialogRoot>

<style>
  .ua-title {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
  }
  .ua-desc {
    font-size: 13px;
    color: var(--color-mid, #aaa);
    line-height: 1.5;
    margin: 0 0 12px;
  }
  .ua-count {
    margin: 0 0 16px;
    font-size: 13px;
  }
  .ua-count strong {
    color: rgb(0, 191, 166);
    font-size: 18px;
  }
  .ua-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    font-size: 13px;
    cursor: pointer;
  }
  .ua-toggle input {
    accent-color: rgb(0, 191, 166);
  }
  .ua-hint {
    margin: 0;
    font-size: 11px;
    color: var(--color-muted, #888);
    line-height: 1.5;
  }
  .ua-running {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    padding: 24px 16px;
    text-align: center;
  }
  .ua-running p {
    margin: 0;
    font-size: 13px;
  }
  .ua-spinner {
    width: 28px;
    height: 28px;
    border: 3px solid rgba(0, 191, 166, 0.2);
    border-top-color: rgb(0, 191, 166);
    border-radius: 50%;
    animation: ua-spin 0.8s linear infinite;
  }
  @keyframes ua-spin {
    to { transform: rotate(360deg); }
  }
  .ua-error {
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 6px;
    padding: 12px;
    color: var(--color-pink, #f99);
    font-size: 12px;
  }
  .ua-error pre {
    margin: 6px 0 0;
    font-size: 11px;
    white-space: pre-wrap;
  }
  .ua-summary {
    margin: 0 0 12px;
    font-size: 13px;
  }
  .ua-summary strong {
    margin: 0 2px;
  }
  .ua-rows {
    list-style: none;
    padding: 0;
    margin: 0;
    max-height: 360px;
    overflow-y: auto;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
  }
  .ua-row {
    display: grid;
    grid-template-columns: 24px 1fr auto;
    gap: 8px;
    align-items: center;
    padding: 8px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    font-size: 12px;
  }
  .ua-row:last-child { border-bottom: none; }
  .ua-row-icon {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    font-weight: 700;
  }
  .ua-row-succeeded .ua-row-icon {
    color: rgb(0, 191, 166);
    background: rgba(0, 191, 166, 0.15);
  }
  .ua-row-failed .ua-row-icon {
    color: var(--color-pink, #f99);
    background: rgba(255, 79, 160, 0.15);
  }
  .ua-row-skipped .ua-row-icon {
    color: var(--color-muted, #888);
    background: rgba(255, 255, 255, 0.06);
  }
  .ua-row-name {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ua-row-status {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: var(--color-mid, #aaa);
  }
  .ua-row-error {
    grid-column: 2 / -1;
    font-size: 11px;
    color: var(--color-pink, #f99);
    background: rgba(255, 79, 160, 0.06);
    padding: 4px 6px;
    border-radius: 3px;
    margin-top: 4px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: help;
  }
  .ua-row-warnings {
    grid-column: 2 / -1;
    font-size: 11px;
    color: #ffc800;
    margin-top: 4px;
  }
  .btn-primary {
    padding: 6px 14px;
    background: rgb(0, 191, 166);
    color: #000;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
  }
  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-ghost {
    padding: 6px 14px;
    background: rgba(255, 255, 255, 0.06);
    color: inherit;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    margin-right: 8px;
  }
  .btn-ghost:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
