<script lang="ts">
  // Compact, passive status pill for the projects-list grid
  // (`routes/project/+page.svelte`). Indicates whether each project's
  // code-graph build is queued / running / failed / etc. without
  // taking up a list-row's full width.
  //
  // **Not** used on the project page itself — that surface uses
  // `CodeGraphBuildBanner.svelte` (full-width, action-bearing, with the
  // "Retry build" button and the failure-detail expansion). This pill is
  // intentionally passive: no click target, no popover, no retry — the
  // user navigates into the project to act on a failure. Keeping the
  // pill display-only avoids two clickable status surfaces fighting
  // (one in the list row, one as a banner once you're inside).
  //
  // History: before 2026-05-12 there was one `CodeGraphBuildPill` that
  // did both list-row AND project-page duty (with a `compact` prop to
  // distinguish). The project-page surface was promoted to a banner for
  // visibility (Decision 2026-05-12 — see
  // `.claude/context/kg-autosync-patch-2026-05-12.md`); this file
  // survives as the passive list-row indicator.

  import { onDestroy, onMount } from 'svelte';
  import { listen, safeInvoke } from '$lib/tauri';
  import type {
    CodeGraphBuildView,
    CodeGraphBuildStatus,
  } from '$lib/types/launcher';

  interface Props {
    projectId: string;
    /** Compat with the pre-banner API: `compact` strips the text label,
     *  rendering only the glyph. The list-row caller always passes
     *  `compact` today. Left as a prop in case a future caller wants a
     *  labelled pill in a denser surface than the banner. */
    compact?: boolean;
  }

  let { projectId, compact = false }: Props = $props();

  let view = $state<CodeGraphBuildView | null>(null);
  let unlisten: (() => void) | null = null;

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
        if (
          e.payload.status === 'success' ||
          e.payload.status === 'partial' ||
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
      case 'partial': return '⚠';
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
      case 'partial':
        return v.files_analyzed === 0
          ? 'Indexed · cleanup warnings'
          : `Indexed · ${v.files_analyzed} file${v.files_analyzed === 1 ? '' : 's'} · cleanup warnings`;
      case 'failed': return 'Index failed';
      case 'skipped': return 'No source files';
    }
  }

  function tooltip(v: CodeGraphBuildView): string {
    const parts: string[] = [`Code graph: ${statusLabel(v)}`];
    if (v.error_message) parts.push(v.error_message);
    if (v.languages.length > 0) parts.push(`Languages: ${v.languages.join(', ')}`);
    if (v.duration_ms != null) parts.push(`Took ${(v.duration_ms / 1000).toFixed(1)}s`);
    return parts.join(' · ');
  }
</script>

{#if view}
  <span
    class="pill status-{view.status}"
    class:compact
    title={tooltip(view)}
    aria-label={`Code graph: ${statusLabel(view)}`}
    role="status"
  >
    <span class="glyph" class:spin={view.status === 'running'} aria-hidden="true">
      {statusGlyph(view.status)}
    </span>
    {#if !compact}
      <span class="label">{statusLabel(view)}</span>
    {/if}
  </span>
{/if}

<style>
  /* Styles cloned from the pre-2026-05-12 pill, minus the failure-
     popover plumbing (this is the passive list-row variant — failure
     details live on the project-page banner instead). */
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
  /* Partial (v0.2.73 C-11): inserts succeeded, stale prune incomplete —
     amber warning, distinct from the pink `failed`. */
  .status-partial {
    background: rgba(245,179,66,0.12);
    border-color: rgba(245,179,66,0.4);
    color: rgb(245,179,66);
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
