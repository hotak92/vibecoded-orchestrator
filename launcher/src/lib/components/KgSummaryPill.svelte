<script lang="ts">
  // Compact, passive status pill for the projects-list grid
  // (`routes/project/+page.svelte`). Mirrors `KgSyncPill.svelte` /
  // `CodeGraphBuildPill.svelte` — same passive design, same list-row
  // scope, same rationale:
  //
  //   * Project-page surface uses `KgSummaryBanner.svelte` (full-width,
  //     action-bearing, with the "Retry" button and the failure-detail
  //     expansion).
  //   * This pill is intentionally passive: no click target, no popover,
  //     no retry. The user navigates into the project to act on a
  //     failure. Keeps the list row uncluttered.
  //
  // The three pills (code-graph + kg-sync + kg-summary) share styles so
  // they line up visually in a row.

  import { onDestroy, onMount } from 'svelte';
  import { listen, safeInvoke } from '$lib/tauri';
  import type { KgSummaryStatus, KgSummaryView } from '$lib/types/launcher';

  interface Props {
    projectId: string;
    /** `compact` strips the text label, rendering only the glyph. The
     *  list-row caller always passes `compact` today. */
    compact?: boolean;
  }

  let { projectId, compact = false }: Props = $props();

  let view = $state<KgSummaryView | null>(null);
  let unlisten: (() => void) | null = null;

  async function load() {
    view = await safeInvoke<KgSummaryView | null>('get_kg_summary_status', { projectId });
  }

  onMount(async () => {
    await load();
    unlisten = await listen<KgSummaryView>(
      'kg-summary-progress',
      (e) => {
        if (e.payload.project_id !== projectId) return;
        view = {
          ...(view ?? {
            project_id: projectId,
            status: e.payload.status,
            started_at_iso: null,
            finished_at_iso: null,
            duration_ms: null,
            nodes_total: 0,
            nodes_succeeded: 0,
            nodes_unchanged: 0,
            nodes_failed: 0,
            nodes_skipped: 0,
            backend: null,
            error_message: null,
            log_tail: null,
            current_phase: null,
          }),
          status: e.payload.status,
          nodes_total: e.payload.nodes_total ?? view?.nodes_total ?? 0,
          nodes_succeeded: e.payload.nodes_succeeded ?? view?.nodes_succeeded ?? 0,
          nodes_unchanged: e.payload.nodes_unchanged ?? view?.nodes_unchanged ?? 0,
          nodes_failed: e.payload.nodes_failed ?? view?.nodes_failed ?? 0,
          nodes_skipped: e.payload.nodes_skipped ?? view?.nodes_skipped ?? 0,
          backend: e.payload.backend ?? view?.backend ?? null,
          current_phase: e.payload.current_phase,
          error_message: e.payload.error_message ?? view?.error_message ?? null,
        };
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

  function statusGlyph(s: KgSummaryStatus): string {
    switch (s) {
      case 'pending': return '·';
      case 'running': return '⟳';
      case 'success': return '✓';
      case 'failed': return '!';
      case 'skipped': return '∅';
    }
  }

  function statusLabel(v: KgSummaryView): string {
    const done =
      v.nodes_succeeded + v.nodes_unchanged + v.nodes_failed + v.nodes_skipped;
    switch (v.status) {
      case 'pending': return 'Sum queued';
      case 'running':
        if (v.current_phase === 'scan') return 'Sum scanning…';
        if (v.nodes_total > 0) return `Sum ${done}/${v.nodes_total}`;
        return 'Sum running…';
      case 'success': {
        const ok = v.nodes_succeeded + v.nodes_unchanged;
        if (v.nodes_total === 0) return 'Sum complete';
        return `Sum ${ok}/${v.nodes_total}`;
      }
      case 'failed': return 'Sum failed';
      case 'skipped': return 'Sum skipped';
    }
  }

  function tooltip(v: KgSummaryView): string {
    const parts: string[] = [`KG summaries: ${statusLabel(v)}`];
    if (v.backend) parts.push(`backend: ${v.backend}`);
    if (v.error_message) parts.push(v.error_message);
    if (v.duration_ms != null) parts.push(`Took ${(v.duration_ms / 1000).toFixed(1)}s`);
    return parts.join(' · ');
  }
</script>

{#if view}
  <span
    class="pill status-{view.status}"
    class:compact
    title={tooltip(view)}
    aria-label={`KG summaries: ${statusLabel(view)}`}
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
  /* Styles cloned verbatim from KgSyncPill / CodeGraphBuildPill so the
     three pills line up visually in the projects-list grid. */
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
