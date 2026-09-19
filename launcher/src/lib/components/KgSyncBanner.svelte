<script lang="ts">
  // Full-width status banner for the initial `kg-sync --all` run kicked
  // off by `create_project_v2` (and re-triggered by `retry_kg_sync`).
  //
  // Was previously `KgSyncPill.svelte`. Promoted to a full-width banner
  // 2026-05-12 for better visibility — for a project with 50+ pre-existing
  // KG nodes the embed pass takes 30-60s, and a small pill in the header
  // didn't make it clear that anything was happening.
  //
  // Behaviour identical to `CodeGraphBuildBanner.svelte` (intentional —
  // see the "two parallel banner components" rationale in
  // `.claude/context/kg-autosync-patch-2026-05-12.md`). The two banners
  // stack vertically in the project page; render order is wired in
  // `routes/project/[id]/+page.svelte`.
  //
  // v0.2.95 R7: the chrome (markup skeleton + ~150 lines of CSS that were
  // a verbatim clone of CodeGraphBuildBanner's) moved to the shared
  // `StatusBannerShell.svelte`. This component keeps its own data, labels
  // and action verbs; nothing about the rendered result changed.

  import { onDestroy, onMount } from 'svelte';
  import { listen, invoke, safeInvoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { KgSyncStatus, KgSyncView } from '$lib/types/launcher';
  import {
    kgSyncBannerRunningLabel,
    kgSyncBannerSuccessLabel,
    kgSyncDoneCount,
    kgSyncTotalCount,
  } from './kg-sync-banner-logic';
  import StatusBannerShell from './StatusBannerShell.svelte';
  import { toneForKgSyncStatus } from './status-banner-tone';

  interface Props {
    projectId: string;
    /** When set, banner stays mounted in terminal states (success/skipped)
     *  for `hideTerminalAfterMs` after `finished_at_iso`, then unmounts.
     *  Defaults to 30s so the user has time to read "Synced N nodes". */
    hideTerminalAfterMs?: number;
  }

  let { projectId, hideTerminalAfterMs = 30_000 }: Props = $props();

  let view = $state<KgSyncView | null>(null);
  let unlisten: (() => void) | null = null;
  let expanded = $state(false);
  let retrying = $state(false);
  let dismissed = $state(false);
  let now = $state(Date.now());
  let tickHandle: ReturnType<typeof setInterval> | null = null;

  async function retry() {
    if (retrying) return;
    retrying = true;
    try {
      // Strict invoke so a failed retry surfaces (safeInvoke swallowed the
      // rejection, making this catch dead and the button a silent no-op).
      await invoke<void>('retry_kg_sync', { projectId });
      expanded = false;
      dismissed = false;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (view) view = { ...view, error_message: msg };
      toast.error(`KG sync retry failed: ${msg}`);
    } finally {
      retrying = false;
    }
  }

  async function load() {
    view = await safeInvoke<KgSyncView | null>('get_kg_sync_status', { projectId });
  }

  function maybeStartTick(v: KgSyncView) {
    const terminal = v.status === 'success' || v.status === 'skipped';
    if (terminal && v.finished_at_iso && tickHandle === null) {
      tickHandle = setInterval(() => { now = Date.now(); }, 1000);
    }
    if (!terminal && tickHandle !== null) {
      clearInterval(tickHandle);
      tickHandle = null;
    }
  }

  onMount(async () => {
    await load();
    if (view) maybeStartTick(view);
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
            kg_skipped: 0,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
            docs_skipped: 0,
            error_message: null,
            log_tail: null,
            current_phase: null,
          }),
          status: e.payload.status,
          kg_total: e.payload.kg_total ?? view?.kg_total ?? 0,
          kg_succeeded: e.payload.kg_succeeded ?? view?.kg_succeeded ?? 0,
          kg_failed: e.payload.kg_failed ?? view?.kg_failed ?? 0,
          kg_skipped: e.payload.kg_skipped ?? view?.kg_skipped ?? 0,
          docs_total: e.payload.docs_total ?? view?.docs_total ?? 0,
          docs_succeeded: e.payload.docs_succeeded ?? view?.docs_succeeded ?? 0,
          docs_failed: e.payload.docs_failed ?? view?.docs_failed ?? 0,
          docs_skipped: e.payload.docs_skipped ?? view?.docs_skipped ?? 0,
          current_phase: e.payload.current_phase,
          error_message: e.payload.error_message ?? view?.error_message ?? null,
        };

        if (
          e.payload.status === 'success' ||
          e.payload.status === 'failed' ||
          e.payload.status === 'skipped'
        ) {
          void load().then(() => { if (view) maybeStartTick(view); });
        }
        if (e.payload.status === 'pending' || e.payload.status === 'running') {
          dismissed = false;
        }
      },
    );
  });

  onDestroy(() => {
    unlisten?.();
    if (tickHandle !== null) clearInterval(tickHandle);
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

  function progressCounter(v: KgSyncView): string {
    // v0.2.92 WP-B1: a file that was intentionally skipped (archived /
    // embed-skip / excluded) is fully ACCOUNTED FOR — count it in `done`
    // so the bar completes honestly instead of stalling at total - skips.
    const done = kgSyncDoneCount(v);
    const total = kgSyncTotalCount(v);
    if (total === 0) return '';
    return `${done} / ${total}`;
  }

  function statusLabel(v: KgSyncView): string {
    switch (v.status) {
      case 'pending':
        return 'KG sync: queued';
      case 'running':
        // v0.2.92 WP-B1: the running-label decision (incl. the new
        // 'finalize' stage and the neutral unknown-phase fallback) lives
        // in kg-sync-banner-logic.ts — unit-tested there.
        return kgSyncBannerRunningLabel(progressCounter(v), v.current_phase);
      case 'success':
        return kgSyncBannerSuccessLabel(v);
      case 'failed':
        return 'KG sync: failed';
      case 'skipped':
        return 'KG sync: no knowledge/ or docs/ content to sync';
    }
  }

  function detailLine(v: KgSyncView): string {
    const parts: string[] = [];
    if (v.kg_total > 0 || v.kg_succeeded > 0) {
      parts.push(`knowledge/: ${v.kg_succeeded}/${v.kg_total}` +
        (v.kg_failed > 0 ? ` (${v.kg_failed} failed)` : ''));
    }
    if (v.docs_total > 0 || v.docs_succeeded > 0) {
      parts.push(`docs/: ${v.docs_succeeded}/${v.docs_total}` +
        (v.docs_failed > 0 ? ` (${v.docs_failed} failed)` : ''));
    }
    if (v.duration_ms != null) parts.push(`Took ${(v.duration_ms / 1000).toFixed(1)}s`);
    return parts.join(' · ');
  }

  let visible = $derived.by(() => {
    if (!view) return false;
    if (dismissed) return false;
    if (view.status === 'failed' || view.status === 'pending' || view.status === 'running') {
      return true;
    }
    if (view.finished_at_iso) {
      const finishedMs = Date.parse(view.finished_at_iso);
      return Number.isFinite(finishedMs) && (now - finishedMs) < hideTerminalAfterMs;
    }
    return true;
  });
</script>

{#if view && visible}
  <!-- v0.2.93 (G): `spinning` also covers OUR retry invoke being pending,
       not only the background poll reporting `running`. -->
  <StatusBannerShell
    tone={toneForKgSyncStatus(view.status)}
    glyph={statusGlyph(view.status)}
    spinning={view.status === 'running' || retrying}
    title={statusLabel(view)}
    detail={detailLine(view)}
    alert={view.status === 'failed'}
    showExpanded={expanded && view.status === 'failed'}
    expandedLabel="KG sync failure detail"
  >
    {#snippet actions()}
      {#if view?.status === 'failed'}
        <button
          type="button"
          class="bg-btn-secondary"
          onclick={() => (expanded = !expanded)}
          aria-expanded={expanded}
        >
          {expanded ? 'Hide details' : 'Show details'}
        </button>
        <button
          type="button"
          class="bg-btn-primary"
          onclick={retry}
          disabled={retrying}
        >
          {#if retrying}
            <span class="bg-glyph-spin" aria-hidden="true">⟳</span> Re-syncing…
          {:else}
            Retry sync
          {/if}
        </button>
      {/if}
      {#if view?.status === 'success' || view?.status === 'skipped'}
        <button
          type="button"
          class="bg-btn-x"
          aria-label="Dismiss banner"
          onclick={() => (dismissed = true)}
        >×</button>
      {/if}
    {/snippet}

    {#snippet expandedContent()}
      <div class="bg-expand-row">
        <strong>Error</strong>
        <pre class="bg-pre">{view?.error_message ?? 'No error message persisted (check launcher logs).'}</pre>
      </div>
      {#if view?.log_tail}
        <div class="bg-expand-row">
          <strong>Log tail</strong>
          <pre class="bg-pre">{view.log_tail}</pre>
        </div>
      {/if}
    {/snippet}
  </StatusBannerShell>
{/if}
