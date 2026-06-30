<script lang="ts">
  // Compact, passive status pill for the projects-list grid
  // (`routes/project/+page.svelte`). Mirrors `CodeGraphBuildPill.svelte`
  // — same passive design, same list-row scope, same rationale:
  //
  //   * Project-page surface uses `KgSyncBanner.svelte` (full-width,
  //     action-bearing, with the "Retry sync" button and the failure-
  //     detail expansion).
  //   * This pill is intentionally passive: no click target, no popover,
  //     no retry. The user navigates into the project to act on a
  //     failure. Keeps the list row uncluttered.
  //
  // See `CodeGraphBuildPill.svelte` for the historical context — same
  // design lineage (Decision 2026-05-12 banner promotion).

  import { onDestroy, onMount } from 'svelte';
  import { listen, safeInvoke } from '$lib/tauri';
  import type { KgSyncStatus, KgSyncView } from '$lib/types/launcher';

  interface Props {
    projectId: string;
    /** `compact` strips the text label, rendering only the glyph. The
     *  list-row caller always passes `compact` today. */
    compact?: boolean;
  }

  let { projectId, compact = false }: Props = $props();

  let view = $state<KgSyncView | null>(null);
  let unlisten: (() => void) | null = null;

  async function load() {
    view = await safeInvoke<KgSyncView | null>('get_kg_sync_status', { projectId });
  }

  onMount(async () => {
    await load();
    unlisten = await listen<KgSyncView>(
      'kg-sync-progress',
      (e) => {
        if (e.payload.project_id !== projectId) return;
        view = {
          ...(view ?? {
            project_id: projectId,
            status: e.payload.status,
            started_at_iso: null,
            finished_at_iso: null,
            duration_ms: null,
            kg_total: 0,
            kg_succeeded: 0,
            kg_failed: 0,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
            error_message: null,
            log_tail: null,
            current_phase: null,
          }),
          status: e.payload.status,
          kg_total: e.payload.kg_total ?? view?.kg_total ?? 0,
          kg_succeeded: e.payload.kg_succeeded ?? view?.kg_succeeded ?? 0,
          kg_failed: e.payload.kg_failed ?? view?.kg_failed ?? 0,
          docs_total: e.payload.docs_total ?? view?.docs_total ?? 0,
          docs_succeeded: e.payload.docs_succeeded ?? view?.docs_succeeded ?? 0,
          docs_failed: e.payload.docs_failed ?? view?.docs_failed ?? 0,
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

  function statusGlyph(s: KgSyncStatus): string {
    switch (s) {
      case 'pending': return '·';
      case 'running': return '⟳';
      case 'success': return '✓';
      case 'failed': return '!';
      case 'skipped': return '∅';
    }
  }

  function statusLabel(v: KgSyncView): string {
    const done = v.kg_succeeded + v.docs_succeeded;
    const total = v.kg_total + v.docs_total;
    switch (v.status) {
      case 'pending': return 'KG queued';
      case 'running':
        if (v.current_phase === 'scan') return 'KG scanning…';
        // v0.2.71 Piece 5a: waiting on the global single-flight embed lane.
        if (v.current_phase === 'queued') return 'KG waiting…';
        if (total > 0) return `KG ${done}/${total}`;
        return 'KG embedding…';
      case 'success':
        if (total === 0) return 'KG synced';
        return `KG ${total} node${total === 1 ? '' : 's'}`;
      case 'failed': return 'KG sync failed';
      case 'skipped': return 'KG no content';
    }
  }

  function tooltip(v: KgSyncView): string {
    const parts: string[] = [`KG sync: ${statusLabel(v)}`];
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
    aria-label={`KG sync: ${statusLabel(view)}`}
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
  /* Styles cloned verbatim from CodeGraphBuildPill so the two pills
     line up visually in the projects-list grid. */
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
