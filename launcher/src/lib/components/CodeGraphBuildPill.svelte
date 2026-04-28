<script lang="ts">
  // Gap 2: status pill for the initial code-graph build.
  //
  // Subscribes to the `code-graph-build-progress` Tauri event for live
  // updates while a build is running, and falls back to
  // `get_code_graph_build_status` for the persisted state on mount /
  // when no event has fired yet. Renders nothing if the project has no
  // build row at all (older projects pre-Gap 2).

  import { onDestroy, onMount } from 'svelte';
  import { listen, safeInvoke } from '$lib/tauri';
  import type {
    CodeGraphBuildView,
    CodeGraphBuildStatus,
  } from '$lib/types/launcher';

  interface Props {
    projectId: string;
    /** Compact mode strips out the timestamp / file count for use in
     *  dense layouts (sidebar list rows). Default false renders the
     *  full pill with hover-tooltip. */
    compact?: boolean;
  }

  let { projectId, compact = false }: Props = $props();

  let view = $state<CodeGraphBuildView | null>(null);
  let unlisten: (() => void) | null = null;
  let showFailureDetail = $state(false);
  let rerunning = $state(false);

  async function rerun() {
    if (rerunning) return;
    rerunning = true;
    try {
      // trigger_code_graph_build is the canonical re-run command — it
      // queues a fresh analysis pass and the live `code-graph-build-progress`
      // event will update our pill state in place.
      await safeInvoke<void>('rebuild_code_graph', { projectId });
      showFailureDetail = false;
    } catch (e) {
      // Surface the error inline rather than a toast — the user is
      // already in a failure-investigation flow.
      if (view) view = { ...view, error_message: e instanceof Error ? e.message : String(e) };
    } finally {
      rerunning = false;
    }
  }

  async function load() {
    view = await safeInvoke<CodeGraphBuildView | null>(
      'get_code_graph_build_status',
      { projectId },
    );
  }

  onMount(async () => {
    await load();
    unlisten = await listen<CodeGraphBuildView>(
      'code-graph-build-progress',
      (e) => {
        if (e.payload.project_id !== projectId) return;
        // Live events only carry status + files_analyzed +
        // current_phase + error. Merge into our local view so we keep
        // any stored timestamps from `get_code_graph_build_status`.
        view = {
          ...(view ?? {
            project_id: projectId,
            status: e.payload.status,
            started_at_iso: null,
            finished_at_iso: null,
            duration_ms: null,
            files_analyzed: 0,
            languages: [],
            joern_used: false,
            error_message: null,
            log_tail: null,
            current_phase: null,
          }),
          status: e.payload.status,
          files_analyzed: e.payload.files_analyzed ?? view?.files_analyzed ?? 0,
          current_phase: e.payload.current_phase,
          error_message: e.payload.error_message ?? view?.error_message ?? null,
        };

        // On terminal events, reload from DB so we pick up the
        // canonical timestamps + languages + log_tail that the Rust
        // side persisted (the event itself omits them to keep the
        // payload small).
        if (
          e.payload.status === 'success' ||
          e.payload.status === 'failed' ||
          e.payload.status === 'skipped'
        ) {
          void load();
        }
      },
    );
  });

  onDestroy(() => {
    unlisten?.();
  });

  function statusGlyph(s: CodeGraphBuildStatus): string {
    switch (s) {
      case 'pending': return '·';
      case 'running': return '⟳';
      case 'success': return '✓';
      case 'failed': return '!';
      case 'skipped': return '∅';
    }
  }

  function statusLabel(v: CodeGraphBuildView): string {
    switch (v.status) {
      case 'pending': return 'Queued';
      case 'running': return v.current_phase === 'scan' ? 'Scanning…' : 'Indexing…';
      case 'success':
        if (v.files_analyzed === 0) return 'Indexed';
        return `Indexed · ${v.files_analyzed} file${v.files_analyzed === 1 ? '' : 's'}`;
      case 'failed': return 'Index failed';
      case 'skipped': return 'No source files';
    }
  }

  function tooltip(v: CodeGraphBuildView): string {
    const parts: string[] = [];
    if (v.error_message) parts.push(v.error_message);
    if (v.languages.length > 0) parts.push(`Languages: ${v.languages.join(', ')}`);
    if (v.duration_ms != null) parts.push(`Took ${(v.duration_ms / 1000).toFixed(1)}s`);
    if (v.finished_at_iso) {
      const d = new Date(v.finished_at_iso);
      parts.push(`Finished ${d.toLocaleString()}`);
    } else if (v.started_at_iso) {
      const d = new Date(v.started_at_iso);
      parts.push(`Started ${d.toLocaleString()}`);
    }
    if (v.joern_used) parts.push('Joern: enabled (CFG + PDG)');
    return parts.join(' · ');
  }
</script>

{#if view}
  <span class="pill-wrap">
    <button
      type="button"
      class="pill status-{view.status}"
      class:compact
      class:clickable={view.status === 'failed'}
      title={tooltip(view)}
      aria-label={`Code graph: ${statusLabel(view)}`}
      onclick={() => {
        if (view?.status === 'failed') showFailureDetail = !showFailureDetail;
      }}
      disabled={view.status !== 'failed'}
    >
      <span class="glyph" class:spin={view.status === 'running'} aria-hidden="true">
        {statusGlyph(view.status)}
      </span>
      {#if !compact}
        <span class="label">{statusLabel(view)}</span>
      {/if}
    </button>

    {#if showFailureDetail && view.status === 'failed'}
      <div class="failure-popover" role="dialog" aria-label="Code graph build failure detail">
        <div class="failure-row">
          <strong>Error</strong>
          <pre class="failure-msg">{view.error_message ?? 'No error message persisted (check launcher logs).'}</pre>
        </div>
        {#if view.log_tail}
          <div class="failure-row">
            <strong>Log tail</strong>
            <pre class="failure-log">{view.log_tail}</pre>
          </div>
        {/if}
        <div class="failure-actions">
          <button type="button" class="btn-secondary" onclick={() => (showFailureDetail = false)}>
            Close
          </button>
          <button type="button" class="btn-primary" onclick={rerun} disabled={rerunning}>
            {rerunning ? 'Retrying…' : 'Retry build'}
          </button>
        </div>
      </div>
    {/if}
  </span>
{/if}

<style>
  .pill-wrap {
    position: relative;
    display: inline-block;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    line-height: 1.4;
    white-space: nowrap;
    border: 1px solid transparent;
    user-select: none;
    background: transparent;
    color: inherit;
    font-family: inherit;
  }
  button.pill {
    cursor: default;
  }
  button.pill.clickable {
    cursor: pointer;
  }
  button.pill.clickable:hover {
    filter: brightness(1.15);
  }
  button.pill[disabled] {
    cursor: default;
  }

  .failure-popover {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    z-index: 1000;
    min-width: 380px;
    max-width: 540px;
    padding: 12px 14px;
    background: #1a1d24;
    border: 1px solid rgba(255,79,160,0.35);
    border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.45);
    font-size: 12px;
    color: #ddd;
  }
  .failure-row {
    margin-bottom: 8px;
  }
  .failure-row strong {
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #999;
    margin-bottom: 4px;
  }
  .failure-msg, .failure-log {
    margin: 0;
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
  }
  .failure-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 10px;
  }
  .btn-secondary, .btn-primary {
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
    font-family: inherit;
  }
  .btn-secondary {
    background: rgba(255,255,255,0.06);
    color: #ccc;
    border-color: rgba(255,255,255,0.12);
  }
  .btn-secondary:hover {
    background: rgba(255,255,255,0.1);
  }
  .btn-primary {
    background: rgb(0,191,166);
    color: #001a17;
  }
  .btn-primary:hover:not(:disabled) {
    background: rgb(0,210,180);
  }
  .btn-primary:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .pill.compact { padding: 1px 6px; gap: 0; }
  .glyph {
    display: inline-block;
    font-family: ui-monospace, monospace;
    font-size: 11px;
  }
  .glyph.spin { animation: spin 1.4s linear infinite; }
  @keyframes spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }

  .status-pending {
    background: rgba(255,255,255,0.06);
    border-color: rgba(255,255,255,0.12);
    color: var(--color-mid, #999);
  }
  .status-running {
    background: rgba(0,191,166,0.10);
    border-color: rgba(0,191,166,0.3);
    color: rgb(0,191,166);
  }
  .status-success {
    background: rgba(70, 200, 120, 0.10);
    border-color: rgba(70, 200, 120, 0.3);
    color: rgb(70, 200, 120);
  }
  .status-failed {
    background: rgba(255,79,160,0.10);
    border-color: rgba(255,79,160,0.3);
    color: rgb(255,79,160);
  }
  .status-skipped {
    background: rgba(245,179,66,0.10);
    border-color: rgba(245,179,66,0.3);
    color: rgb(245,179,66);
  }
</style>
